from __future__ import division

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np
from pytools import memoize_method, Record
import pyopencl as cl
import pyopencl.array
from pyopencl.elementwise import ElementwiseTemplate
from mako.template import Template
from boxtree.tools import AXIS_NAMES, FromDeviceGettableRecord

import logging
logger = logging.getLogger(__name__)




# {{{ preamble

TRAVERSAL_PREAMBLE_TEMPLATE = r"""//CL//
${box_flags_enum.get_c_defines()}
${box_flags_enum.get_c_typedef()}

typedef ${dtype_to_ctype(box_id_dtype)} box_id_t;
%if particle_id_dtype is not None:
    typedef ${dtype_to_ctype(particle_id_dtype)} particle_id_t;
%endif
typedef ${dtype_to_ctype(coord_dtype)} coord_t;
typedef ${dtype_to_ctype(vec_types[coord_dtype, dimensions])} coord_vec_t;

#define NLEVELS ${max_levels}

<%def name="load_center(name, box_id)">
    coord_vec_t ${name};
    %for i in range(dimensions):
        ${name}.${AXIS_NAMES[i]} = box_centers[aligned_nboxes * ${i} + ${box_id}];
    %endfor
</%def>

#define LEVEL_TO_SIZE(level) \
        (root_extent * 1 / (coord_t) (1 << level))

%if 0:
    #define dbg_printf(ARGS) printf ARGS
%else:
    #define dbg_printf(ARGS) /* */
%endif

<%def name="walk_init(start_box_id)">
    box_id_t box_stack[NLEVELS];
    int morton_nr_stack[NLEVELS];

    // start at root
    int walk_level = 0;
    box_id_t walk_box_id = ${start_box_id};
    int walk_morton_nr = 0;
    bool continue_walk = true;
</%def>

<%def name="walk_advance()">
    while (true)
    {
        ++walk_morton_nr;
        if (walk_morton_nr < ${2**dimensions})
            break;

        // Ran out of children, pull the next guy off the stack
        // and advance him.

        continue_walk = walk_level > 0;
        if (continue_walk)
        {
            --walk_level;
            dbg_printf(("    ascend\n"));
            walk_box_id = box_stack[walk_level];
            walk_morton_nr = morton_nr_stack[walk_level];
        }
        else
        {
            dbg_printf(("done\n"));
            break;
        }
    }
</%def>

<%def name="walk_push()">
    box_stack[walk_level] = walk_box_id;
    morton_nr_stack[walk_level] = walk_morton_nr;
    ++walk_level;

    %if debug:
    if (walk_level >= NLEVELS)
    {
        dbg_printf(("  ** ERROR: overran levels stack\n"));
        return;
    }
    %endif
</%def>

"""

# }}}

# {{{ helper functions (adjacency test, ...)

HELPER_FUNCTION_TEMPLATE = r"""//CL//

bool is_adjacent_or_overlapping(
    USER_ARG_DECL coord_vec_t center, int level, box_id_t other_box_id)
{
    ${load_center("other_center", "other_box_id")}
    int other_level = box_levels[other_box_id];

    // This checks if the two boxes overlap
    // with an amount of 'slack' corresponding to half the
    // width of the smaller of the two boxes.
    // (Without the 'slack', there wouldn't be any
    // overlap.)

    coord_t size_sum = 0.5 * (LEVEL_TO_SIZE(level) + LEVEL_TO_SIZE(other_level));
    coord_t slack = size_sum + 0.5 * LEVEL_TO_SIZE(max(level, other_level));

    coord_t max_dist = 0;
    %for i in range(dimensions):
        max_dist = fmax(max_dist, fabs(center.s${i} - other_center.s${i}));
    %endfor

    return max_dist <= slack;
}

"""

# }}}

# {{{ sources and their parents, targets

SOURCES_PARENTS_AND_TARGETS_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t box_id)
{
    box_flags_t flags = box_flags[box_id];

    if (flags & BOX_HAS_OWN_SOURCES)
    { APPEND_source_boxes(box_id); }

    if (flags & BOX_HAS_CHILD_SOURCES)
    { APPEND_source_parent_boxes(box_id); }

    %if not sources_are_targets:
        if (flags & BOX_HAS_OWN_TARGETS)
        { APPEND_target_boxes(box_id); }
    %endif
}
"""

# }}}

# {{{ level start box nrs

LEVEL_START_BOX_NR_EXTRACTOR_TEMPLATE = ElementwiseTemplate(
    arguments="""//CL//
    box_id_t *level_start_box_nrs,
    box_level_t *box_levels,
    box_id_t *box_list,
    box_id_t *list_level_start_box_nrs,
    """,

    operation=r"""//CL//
        // Kernel is ranged so that this is true:
        // assert(i > 0);

        box_id_t my_box_id = box_list[i];
        box_id_t prev_box_id = box_list[i-1];

        int my_level = box_levels[my_box_id];
        box_id_t my_level_start = level_start_box_nrs[my_level];

        if (prev_box_id < my_level_start && my_level_start <= my_box_id)
            list_level_start_box_nrs[my_level] = i;
    """,
    name="extract_level_start_box_nrs")

# }}}

# {{{ colleagues

COLLEAGUES_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t box_id)
{
    ${load_center("center", "box_id")}

    if (box_id == 0)
    {
        // The root has no colleagues.
        return;
    }

    int level = box_levels[box_id];

    dbg_printf(("box id: %d level: %d\n", box_id, level));

    // To find this box's colleagues, start at the top of the tree, descend
    // into adjacent (or overlapping) parents.
    ${walk_init(0)}

    while (continue_walk)
    {
        box_id_t child_box_id = box_child_ids[walk_morton_nr * aligned_nboxes + walk_box_id];
        dbg_printf(("  level: %d walk box id: %d morton: %d child id: %d\n",
            walk_level, walk_box_id, walk_morton_nr, child_box_id));

        if (child_box_id)
        {
            bool a_or_o = is_adjacent_or_overlapping(
                USER_ARGS center, level, child_box_id);

            if (a_or_o)
            {
                // child_box_id lives on walk_level+1.
                if (walk_level+1 == level  && child_box_id != box_id)
                {
                    dbg_printf(("    colleague\n"));
                    APPEND_colleagues(child_box_id);
                }
                else
                {
                    // We want to descend into this box. Put the current state
                    // on the stack.

                    dbg_printf(("    descend\n"));
                    ${walk_push()}

                    walk_box_id = child_box_id;
                    walk_morton_nr = 0;
                    continue;
                }
            }
            else
            {
                dbg_printf(("    not adjacent\n"));
            }
        }

        ${walk_advance()}
    }
}

"""

# }}}

# {{{ neighbor source boxes ("list 1")

NEIGBHOR_SOURCE_BOXES_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t target_box_number)
{
    // /!\ target_box_number is *not* a box_id, despite the type.
    // It's the number of the source box we're currently processing.

    box_id_t box_id = target_boxes[target_box_number];

    ${load_center("center", "box_id")}

    int level = box_levels[box_id];

    dbg_printf(("box id: %d level: %d\n", box_id, level));

    // To find this box's colleagues, start at the top of the tree, descend
    // into adjacent (or overlapping) parents.
    ${walk_init(0)}

    while (continue_walk)
    {
        box_id_t child_box_id = box_child_ids[walk_morton_nr * aligned_nboxes + walk_box_id];
        dbg_printf(("  walk box id: %d morton: %d child id: %d level: %d\n",
            walk_box_id, walk_morton_nr, child_box_id, walk_level));

        if (child_box_id)
        {
            bool a_or_o = is_adjacent_or_overlapping(
                USER_ARGS center, level, child_box_id);

            if (a_or_o)
            {
                box_flags_t flags = box_flags[child_box_id];
                /* child_box_id == box_id is ok */
                if (flags & BOX_HAS_OWN_SOURCES)
                {
                    dbg_printf(("    neighbor source box\n"));

                    APPEND_neighbor_source_boxes(child_box_id);
                }

                if (flags & BOX_HAS_CHILD_SOURCES)
                {
                    // We want to descend into this box. Put the current state
                    // on the stack.

                    dbg_printf(("    descend\n"));
                    ${walk_push()}

                    walk_box_id = child_box_id;
                    walk_morton_nr = 0;
                    continue;
                }
            }
            else
            {
                dbg_printf(("    not adjacent\n"));
            }
        }

        ${walk_advance()}
    }
}

"""

# }}}

# {{{ well-separated siblings ("list 2")

SEP_SIBLINGS_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t box_id)
{
    ${load_center("center", "box_id")}

    int level = box_levels[box_id];

    box_id_t parent = box_parent_ids[box_id];
    if (parent == box_id)
        return;

    box_id_t parent_coll_start = colleagues_starts[parent];
    box_id_t parent_coll_stop = colleagues_starts[parent+1];

    // /!\ i is not a box_id, it's an index into colleagues_list.
    for (box_id_t i = parent_coll_start; i < parent_coll_stop; ++i)
    {
        box_id_t parent_colleague = colleagues_list[i];

        for (int morton_nr = 0; morton_nr < ${2**dimensions}; ++morton_nr)
        {
            box_id_t sib_box_id = box_child_ids[
                    morton_nr * aligned_nboxes + parent_colleague];

            bool sep = !is_adjacent_or_overlapping(
                USER_ARGS center, level, sib_box_id);

            if (sep)
            {
                APPEND_sep_siblings(sib_box_id);
            }
        }
    }
}
"""

# }}}

# {{{ separated smaller non-siblings ("list 3")

SEP_SMALLER_NONSIBLINGS_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t target_box_number)
{
    // /!\ target_box_number is *not* a box_id, despite the type.
    // It's the number of the target box we're currently processing.

    box_id_t box_id = target_boxes[target_box_number];

    ${load_center("center", "box_id")}

    int level = box_levels[box_id];

    box_id_t coll_start = colleagues_starts[box_id];
    box_id_t coll_stop = colleagues_starts[box_id+1];

    // /!\ i is not a box_id, it's an index into colleagues_list.
    for (box_id_t i = coll_start; i < coll_stop; ++i)
    {
        box_id_t colleague = colleagues_list[i];

        ${walk_init("colleague")}

        while (continue_walk)
        {
            // Loop invariant: walk_box_id is always adjacent to box_id.
            // This is true at the first level because colleagues are by adjacent
            // by definition, and is kept true throughout the walk by only descending
            // into adjacent boxes.

            box_id_t child_box_id = box_child_ids[walk_morton_nr * aligned_nboxes + walk_box_id];
            dbg_printf(("  walk box id: %d morton: %d child id: %d level: %d\n",
                walk_box_id, walk_morton_nr, child_box_id, walk_level));

            if (child_box_id)
            {
                bool a_or_o = is_adjacent_or_overlapping(
                    USER_ARGS center, level, child_box_id);

                if (a_or_o)
                {
                    if (box_flags[child_box_id] & BOX_HAS_CHILDREN)
                    {
                        // We want to descend into this box. Put the current state
                        // on the stack.

                        dbg_printf(("    descend\n"));
                        ${walk_push()}

                        walk_box_id = child_box_id;
                        walk_morton_nr = 0;
                        continue;
                    }
                }
                else
                {
                    APPEND_sep_smaller_nonsiblings(child_box_id);
                }
            }

            ${walk_advance()}
        }
    }
}
"""

# }}}

# {{{ separated bigger non-siblings ("list 4")

SEP_BIGGER_NONSIBLINGS_TEMPLATE = r"""//CL//

void generate(LIST_ARG_DECL USER_ARG_DECL box_id_t box_id)
{
    ${load_center("center", "box_id")}

    int box_level = box_levels[box_id];

    box_id_t current_parent_box_id = box_id;
    int walk_level = box_level;

    while (walk_level)
    {
        // {{{ advance

        --walk_level;

        // Box 0 (== level 0) doesn't have any colleagues.
        if (walk_level == 0)
            break;

        current_parent_box_id = box_parent_ids[current_parent_box_id];

        // }}}

        box_id_t coll_start = colleagues_starts[current_parent_box_id];
        box_id_t coll_stop = colleagues_starts[current_parent_box_id+1];

        // /!\ i is not a box_id, it's an index into colleagues_list.
        for (box_id_t i = coll_start; i < coll_stop; ++i)
        {
            box_id_t colleague_box_id = colleagues_list[i];

            bool a_or_o = is_adjacent_or_overlapping(
                USER_ARGS center, box_level, colleague_box_id);

            if (!a_or_o && box_flags[colleague_box_id] & BOX_HAS_OWN_SOURCES)
            {
                // Now check if any of box_id's parents up to current_parent_box_id
                // are already not adjacent to colleague_box_id. If so, then that parent
                // will have included colleague_box_id in its List 4, and it will
                // propagate down by local expansion. Therefore, no need to add in
                // that case.
                //
                // Yes, Batman, we just went O(n log(n)^2).

                ${load_center("colleague_center", "colleague_box_id")}

                box_id_t check_parent_box_id = box_id;
                int check_walk_level = box_level;

                bool found_closer_parent = false;

                while (true)
                {
                    // {{{ advance

                    --check_walk_level;

                    if (check_walk_level == walk_level)
                        break;

                    check_parent_box_id = box_parent_ids[check_parent_box_id];

                    // }}}

                    bool parent_a_or_o = is_adjacent_or_overlapping(
                        USER_ARGS colleague_center, walk_level, check_parent_box_id);

                    if (!parent_a_or_o)
                    {
                        found_closer_parent = true;
                        break;
                    }
                }

                if (!found_closer_parent)
                {
                    APPEND_sep_bigger_nonsiblings(colleague_box_id);
                }
            }
        }
    }
}
"""

# }}}

# {{{ traversal info (output)

class FMMTraversalInfo(FromDeviceGettableRecord):
    """
    .. attribute:: tree

        An instance of :class:`boxtree.Tree`.

    .. attribute:: source_boxes

        ``box_id_t [*]`` List of boxes having sources.

    .. attribute:: target_boxes

        ``box_id_t [*]`` List of boxes having sources.
        If :attr:`boxtree.Tree.sources_are_targets`,
        then ``target_boxes is source_boxes``.

    .. attribute:: source_parent_boxes

        ``box_id_t [*]`` List of boxes that are (directly or indirectly) a parent
        of one of the :attr:`source_boxes`. These boxes may have sources of their
        own.

    .. attribute:: level_start_source_parent_box_nrs

        ``box_id_t [nlevels+1]``
        Indices into :attr:`source_parent_boxes` indicating where
        each level starts and ends.

    For each of the following data structures, the `starts` part
    contains indices into the `lists` part.

    .. attribute:: colleagues_starts

        ``box_id_t [nboxes+1]``
    .. attribute:: colleagues_lists

        ``box_id_t [*]``

    **"List 1"**

    .. attribute:: neighbor_source_boxes_starts

        ``box_id_t [ntarget_boxes+1]``

    .. attribute:: neighbor_source_boxes_lists

        ``box_id_t [*]``

    **"List 2"**

    .. attribute:: sep_siblings_starts

        ``box_id_t [nboxes+1]``

    .. attribute:: sep_siblings_lists

        ``box_id_t [*]``

    **"List 3"**

    .. attribute:: sep_smaller_nonsiblings_starts

        ``box_id_t [ntargets+1]``

    .. attribute:: sep_smaller_nonsiblings_lists

        ``box_id_t [*]``

    **"List 4"**

    .. attribute:: sep_bigger_nonsiblings_starts

        ``box_id_t [nboxes+1]``

    .. attribute:: sep_bigger_nonsiblings_lists

        ``box_id_t [*]``

    Terminology follows this article:

    Carrier, J., Greengard, L. and Rokhlin, V. "A Fast
    Adaptive Multipole Algorithm for Particle Simulations." SIAM Journal on
    Scientific and Statistical Computing 9, no. 4 (July 1988): 669-686.
    `DOI: 10.1137/0909044 <http://dx.doi.org/10.1137/0909044>`_.
    """

    # {{{ debugging aids

    def get_box_list(self, what, index):
        starts = getattr(self, what+"_starts")
        lists = getattr(self, what+"_lists")
        start, stop = starts[index:index+2]
        return lists[start:stop]

    # }}}

# }}}

class _KernelInfo(Record):
    pass

# {{{ top-level

class FMMTraversalBuilder:
    def __init__(self, context):
        self.context = context

    # {{{ kernel builder

    @memoize_method
    def get_kernel_info(self, dimensions, particle_id_dtype, box_id_dtype,
            coord_dtype, box_level_dtype, max_levels,
            sources_are_targets):

        logging.info("building traversal build kernels")

        debug = False

        from pyopencl.tools import dtype_to_ctype
        from boxtree.tree import box_flags_enum
        render_vars = dict(
                dimensions=dimensions,
                dtype_to_ctype=dtype_to_ctype,
                particle_id_dtype=particle_id_dtype,
                box_id_dtype=box_id_dtype,
                box_flags_enum=box_flags_enum,
                coord_dtype=coord_dtype,
                vec_types=cl.array.vec.types,
                max_levels=max_levels,
                AXIS_NAMES=AXIS_NAMES,
                debug=debug,
                sources_are_targets=sources_are_targets,
                )
        from pyopencl.algorithm import ListOfListsBuilder
        from pyopencl.tools import VectorArg, ScalarArg

        result = {}

        # {{{ source boxes, their parents, target boxes

        src = Template(
                TRAVERSAL_PREAMBLE_TEMPLATE
                + SOURCES_PARENTS_AND_TARGETS_TEMPLATE,
                strict_undefined=True).render(**render_vars)

        result["sources_parents_and_targets_builder"] = \
                ListOfListsBuilder(self.context,
                        [
                            ("source_parent_boxes", box_id_dtype),
                            ("source_boxes", box_id_dtype),
                            ] + (
                                [("target_boxes", box_id_dtype)]
                                if not sources_are_targets
                                else []),
                        str(src),
                        arg_decls=[
                            VectorArg(box_flags_enum.dtype, "box_flags"),
                            ], debug=debug, name_prefix="sources_and_parents")

        result["level_start_box_nrs_extractor"] = \
                LEVEL_START_BOX_NR_EXTRACTOR_TEMPLATE.build(self.context,
                    type_aliases=(
                        ("box_id_t", box_id_dtype),
                        ("box_level_t", box_level_dtype),
                        ),
                    )

        # }}}

        # {{{ build list N builders

        base_args = [
                VectorArg(coord_dtype, "box_centers"),
                ScalarArg(coord_dtype, "root_extent"),
                VectorArg(np.uint8, "box_levels"),
                ScalarArg(box_id_dtype, "aligned_nboxes"),
                VectorArg(box_id_dtype, "box_child_ids"),
                VectorArg(box_flags_enum.dtype, "box_flags"),
                ]

        for list_name, template, extra_args in [
                ("colleagues", COLLEAGUES_TEMPLATE, []),
                ("neighbor_source_boxes", NEIGBHOR_SOURCE_BOXES_TEMPLATE,
                        [
                            VectorArg(box_id_dtype, "target_boxes"),
                            ]),
                ("sep_siblings", SEP_SIBLINGS_TEMPLATE,
                        [
                            VectorArg(box_id_dtype, "box_parent_ids"),
                            VectorArg(box_id_dtype, "colleagues_starts"),
                            VectorArg(box_id_dtype, "colleagues_list"),
                            ]),
                ("sep_smaller_nonsiblings", SEP_SMALLER_NONSIBLINGS_TEMPLATE,
                        [
                            VectorArg(box_id_dtype, "target_boxes"),
                            VectorArg(box_id_dtype, "colleagues_starts"),
                            VectorArg(box_id_dtype, "colleagues_list"),
                            ]),
                ("sep_bigger_nonsiblings", SEP_BIGGER_NONSIBLINGS_TEMPLATE,
                        [
                            VectorArg(box_id_dtype, "box_parent_ids"),
                            VectorArg(box_id_dtype, "colleagues_starts"),
                            VectorArg(box_id_dtype, "colleagues_list"),
                            ]),
                ]:
            src = Template(
                    TRAVERSAL_PREAMBLE_TEMPLATE
                    + HELPER_FUNCTION_TEMPLATE
                    + template,
                    strict_undefined=True).render(**render_vars)

            result[list_name+"_builder"] = ListOfListsBuilder(self.context,
                    [(list_name, box_id_dtype) ],
                    str(src),
                    arg_decls=base_args + extra_args,
                    debug=debug, name_prefix=list_name,
                    complex_kernel=True)

        # }}}

        logging.info("traversal build kernels built")

        return _KernelInfo(**result)

    # }}}

    # {{{ driver

    def __call__(self, queue, tree, wait_for=None, debug=False):
        """
        :arg queue: A :class:`pyopencl.CommandQueue` instance.
        :arg tree: A :class:`boxtree.Tree` instance.
        :arg wait_for: may either be *None* or a list of :class:`pyopencl.Event`
            instances for whose completion this command waits before starting
            exeuction.
        :return: A tuple *(trav, event)*, where *trav* is a new instance of
            :class:`FMMTraversalInfo`.  and *event* is a :class:`pyopencl.Event`
            for dependency management.
        """

        if not tree._is_pruned:
            raise ValueError("tree must be pruned for traversal generation")

        from pytools import div_ceil
        max_levels = div_ceil(tree.nlevels, 10) * 10

        knl_info = self.get_kernel_info(
                tree.dimensions, tree.particle_id_dtype, tree.box_id_dtype,
                tree.coord_dtype, tree.box_level_dtype, max_levels,
                tree.sources_are_targets)

        def fin_debug(s):
            if debug:
                queue.finish()

            logger.debug(s)

        logger.info("start building traversal")

        # {{{ source boxes, their parents, and target boxes

        fin_debug("building list of source boxes, their parents, and target boxes")

        result, evt = knl_info.sources_parents_and_targets_builder(
                queue, tree.nboxes, tree.box_flags.data, wait_for=wait_for)
        wait_for = [evt]

        source_boxes = result["source_boxes"].lists
        assert len(source_boxes) == result["source_boxes"].count
        if not tree.sources_are_targets:
            target_boxes = result["target_boxes"].lists
            assert len(target_boxes) == result["target_boxes"].count
        else:
            target_boxes = source_boxes

        source_parent_boxes = result["source_parent_boxes"].lists
        assert len(source_parent_boxes) == result["source_parent_boxes"].count

        # }}}

        # {{{ figure out level starts in source_parent_boxes

        def extract_level_start_box_nrs(box_list, wait_for):
            result = cl.array.empty(queue,
                    tree.nlevels+1, tree.box_id_dtype) \
                            .fill(len(box_list))
            evt = knl_info.level_start_box_nrs_extractor(
                    tree.level_start_box_nrs_dev,
                    tree.box_levels,
                    box_list,
                    result,
                    range=slice(1, len(box_list)),
                    queue=queue, wait_for=wait_for)

            result = result.get()

            # We skipped box 0 above. This is always true, whether
            # box 0 (=level 0) is a leaf or a parent.
            result[0] = 0

            # Postprocess result for unoccupied levels
            prev_start = len(box_list)
            for ilev in xrange(tree.nlevels-1, -1, -1):
                result[ilev] = prev_start = \
                        min(result[ilev], prev_start)

            return result, evt

        fin_debug("finding level starts in source parent boxes array")

        level_start_source_parent_box_nrs, evt = \
                extract_level_start_box_nrs(source_parent_boxes, wait_for=wait_for)

        wait_for = [evt]

        # }}}

        # {{{ colleagues

        fin_debug("finding colleagues")

        result, evt = knl_info.colleagues_builder(
                queue, tree.nboxes,
                tree.box_centers.data, tree.root_extent, tree.box_levels.data,
                tree.aligned_nboxes, tree.box_child_ids.data, tree.box_flags.data,
                wait_for=wait_for)
        wait_for = [evt]
        colleagues = result["colleagues"]

        # }}}

        # {{{ neighbor source boxes ("list 1")

        fin_debug("finding neighbor source boxes ('list 1')")

        result, evt = knl_info.neighbor_source_boxes_builder(
                queue, len(target_boxes),
                tree.box_centers.data, tree.root_extent, tree.box_levels.data,
                tree.aligned_nboxes, tree.box_child_ids.data, tree.box_flags.data,
                target_boxes.data, wait_for=wait_for)
        wait_for = [evt]
        neighbor_source_boxes = result["neighbor_source_boxes"]

        # }}}

        # {{{ well-separated siblings ("list 2")

        fin_debug("finding well-separated siblings ('list 2')")

        result, evt = knl_info.sep_siblings_builder(
                queue, tree.nboxes,
                tree.box_centers.data, tree.root_extent, tree.box_levels.data,
                tree.aligned_nboxes, tree.box_child_ids.data, tree.box_flags.data,
                tree.box_parent_ids.data,
                colleagues.starts.data, colleagues.lists.data, wait_for=wait_for)
        wait_for = [evt]
        sep_siblings = result["sep_siblings"]

        # }}}

        # {{{ separated smaller non-siblings ("list 3")

        fin_debug("finding separated smaller non-siblings ('list 3')")

        result, evt = knl_info.sep_smaller_nonsiblings_builder(
                queue, len(target_boxes),
                tree.box_centers.data, tree.root_extent, tree.box_levels.data,
                tree.aligned_nboxes, tree.box_child_ids.data, tree.box_flags.data,
                target_boxes.data,
                colleagues.starts.data, colleagues.lists.data,
                wait_for=wait_for)
        wait_for = [evt]
        sep_smaller_nonsiblings = result["sep_smaller_nonsiblings"]

        # }}}

        # {{{ separated bigger non-siblings ("list 4")

        fin_debug("finding separated bigger non-siblings ('list 4')")

        result, evt = knl_info.sep_bigger_nonsiblings_builder(
                queue, tree.nboxes,
                tree.box_centers.data, tree.root_extent, tree.box_levels.data,
                tree.aligned_nboxes, tree.box_child_ids.data, tree.box_flags.data,
                tree.box_parent_ids.data,
                colleagues.starts.data, colleagues.lists.data, wait_for=wait_for)
        wait_for=[evt]
        sep_bigger_nonsiblings = result["sep_bigger_nonsiblings"]

        # }}}

        evt, = wait_for

        logger.info("traversal built")

        return FMMTraversalInfo(
                tree=tree,

                source_boxes=source_boxes,
                target_boxes=target_boxes,
                source_parent_boxes=source_parent_boxes,
                level_start_source_parent_box_nrs=level_start_source_parent_box_nrs,

                colleagues_starts=colleagues.starts,
                colleagues_lists=colleagues.lists,

                neighbor_source_boxes_starts=neighbor_source_boxes.starts,
                neighbor_source_boxes_lists=neighbor_source_boxes.lists,

                sep_siblings_starts=sep_siblings.starts,
                sep_siblings_lists=sep_siblings.lists,

                sep_smaller_nonsiblings_starts=sep_smaller_nonsiblings.starts,
                sep_smaller_nonsiblings_lists=sep_smaller_nonsiblings.lists,

                sep_bigger_nonsiblings_starts=sep_bigger_nonsiblings.starts,
                sep_bigger_nonsiblings_lists=sep_bigger_nonsiblings.lists,
                ), evt

    # }}}

# }}}




# vim: filetype=pyopencl:fdm=marker
