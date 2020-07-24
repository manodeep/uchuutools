#!/usr/bin/env python
from __future__ import print_function

__author__ = "Manodeep Sinha"
__all__ = ("read_locations_and_forests", "get_aggregate_forest_info",
           "get_all_parallel_ctrees_filenames",
           "check_forests_locations_filenames",
           "validate_inputs_are_ctrees_files",
           "get_treewalk_dtype_descr", "add_tree_walk_indices", )

import time

from utils import generic_reader, check_and_decompress, get_metadata


# requires numpy >= 1.9.0
def read_locations_and_forests(forests_fname, locations_fname, rank=0):
    """
    Returns a numpy structured array that contains *both* the forest and
    tree level info.

    Parameters
    ----------

    forests_fname: string, required
        The name of the file containing forest-level info like the
        Consistent-Trees 'forests.list' file

    locations_fname: string, required
        The name of the file containing tree-level info like the
        Consistent-Trees 'locations.dat' file

    rank: integer, optional, default:0
        An integer identifying which task is calling this function. Only
        used in status messages

    Returns
    -------

    trees_and_locations: A numpy structured array
        A numpy structured array containing the fields
        <TreeRootID ForestID Filename FileID Offset TreeNbytes>
        The array is sorted by ``(ForestID, Filename, Offset)`` in that order.
        This sorting means that *all* trees belonging to the same forest
        *will* appear consecutively regardless of the file that the
        corresponding tree data might appear in. The number of elements
        in the array is equal to the number of trees.

        Note: Sorting by ``Filename`` is implemented by an equivalent, but
        faster sorting on ``FileID``.

    """
    import os  # use os.stat to get filesize
    import numpy as np
    import numpy.lib.recfunctions as rfn

    def _guess_dtype_from_val(val):
        converters_and_type = [(np.float, np.float),
                               (str, 'U1024')]
        fallback_dtype = 'V'
        for (conv, dtype) in converters_and_type:
            try:
                _ = conv(val)
                return dtype
            except ValueError:
                pass

        print(f"Warning: Could not guess datatype for val = '{val}'. "
              f"Returning datatype = '{fallback_dtype}'")
        return fallback_dtype

    def _get_dtype_from_header(fname):
        with generic_reader(fname, 'r') as f:
            f = iter(f)
            hdr = next(f)
            hdr = hdr.strip('#').rstrip()
            dataline = next(f)

        known_fields = {
            'Offset': np.int64,
            'Filename': 'U512',
        }

        # Now split on whitespace
        fields = hdr.split()
        datavals = dataline.split()
        fields_and_type = [None]*len(fields)
        for ii, (fld, val) in enumerate(zip(fields, datavals)):
            if 'ID' in fld.upper():
                dtype = np.int64
            else:
                try:
                    dtype = known_fields[fld]
                except KeyError:
                    dtype = _guess_dtype_from_val(val)
                    print(f"Warning: Did not find key = '{fld}' in the known "
                          f"columns. Assuming '{dtype}'")
                    pass

            fields_and_type[ii] = (fld, dtype)

        return np.dtype(fields_and_type)

    # The main `read_locations_and_forests` function begins here
    forests_dtype = _get_dtype_from_header(forests_fname)
    locations_dtype = _get_dtype_from_header(locations_fname)

    # Check that both the dtypes have the 'common' field that
    # we are later going to use to join the two files
    join_key = 'TreeRootID'
    if join_key not in forests_dtype.names or \
       join_key not in locations_dtype.names:
        msg = f"Error: Expected to find column = '{join_key}' in "\
              f"both the forests file ('{forests_fname}') and "\
              f"the locations file ('{locations_fname}')"
        msg += f"Columns in forests file = {forests_dtype.names}"
        msg += f"Columns in locations file = {locations_dtype.names}"
        raise KeyError(msg)

    t0 = time.perf_counter()
    print(f"[Rank={rank}]: Reading forests file '{forests_fname}'")
    forests = np.loadtxt(forests_fname, comments='#', dtype=forests_dtype)
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: Reading forests file '{forests_fname}'...done. "
          f"Time taken = {t1-t0:.2f} seconds")

    t0 = time.perf_counter()
    print(f"[Rank={rank}]: Reading locations file '{locations_fname}' ...")
    locations = np.loadtxt(locations_fname, comments='#',
                           dtype=locations_dtype)
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: Reading locations file '{locations_fname}' "
          f"...done. Time taken = {t1-t0:.2f} seconds")

    # Check that the base directory is the same
    # for the 'forests.list' and 'locations.dat' files
    # Otherwise, since the code the code logic will fail in the conversion
    dirname = list(set([os.path.dirname(f) for f in [forests_fname,
                                                     locations_fname]]))
    if len(dirname) != 1:
        msg = "Error: Standard Consistent-Trees catalogs *should* "\
              "create the 'forests.list' and 'locations.dat' in the "\
              f"same directory. Instead found directories = {dirname}\n"\
              f"Input files were = ({forests_fname}, {locations_fname})"
        raise ValueError(msg)

    dirname = dirname[0]
    # The 'locations.dat' file does not have fully-qualified paths
    # Add the prefix so all future queries woork out just fine
    filenames = [f'{dirname}/{fname}' for fname in locations['Filename']
                 if '/' not in fname]
    locations['Filename'][:] = filenames

    t0 = time.perf_counter()
    print(f"[Rank={rank}]: Joining the forests and locations arrays...")
    trees_and_locations = rfn.join_by(join_key, forests, locations,
                                      jointype='inner')
    if trees_and_locations.shape[0] != forests.shape[0]:
        raise AssertionError("Error: Inner join failed to preserve the shape")

    ntrees = trees_and_locations.shape[0]
    treenbytes = np.zeros(ntrees, dtype=np.int64)
    trees_and_locations = rfn.append_fields(trees_and_locations,
                                            'TreeNbytes',
                                            treenbytes)
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: Joining the forests and locations arrays...done. "
          f"Time taken = {t1-t0:.2f} seconds")

    # All the fields have been assigned into the combined structure
    # Now, we need to figure out the bytes size of each forest by sorting on
    # FileID and Offset. The locations.dat file contains the offset where
    # the tree data starts but does not contain where the tree data ends. By
    # sorting on ('FileID', 'Offset'), we can have a guess at where the current
    # tree ends by looking at where the next tree starts, and then removing all
    # intervening bytes. Here, the intervening bytes are what's written out by
    # this line -> https://bitbucket.org/pbehroozi/consistent-trees/src/9668e1f4d396dcecdcd1afab6ac0c80cbd561d72/src/assemble_halo_trees.c#lines-202
    #
    #   fprintf(tree_outputs[file_id], "#tree %"PRId64"\n", halos[0].id);
    #
    # Since we already have the TreeRootID, we know *exactly* how many bytes
    # are taken up by that printf statement. We subtract that many bytes from
    # the next-tree-starting-offset, then we have the
    # current-tree-ending-offset. The last tree in the file will have to
    # special-cased -- in this case, the end of tree data is known to
    # be at end-of-file (EOF). -- MS 16/04/2020

    # Sort the array on filename, and then by offset
    # Sorting by fileID is faster than sorting by filename
    trees_and_locations.sort(order=['FileID', 'Offset'])

    # Calculate the number of bytes in every tree
    t0 = time.perf_counter()
    print(f"[Rank={rank}]: Computing number of bytes per tree...")
    nexttid = trees_and_locations[join_key][1:]
    nexttidlen = np.array([len(f"#tree {x:d}\n") for x in nexttid],
                          dtype=np.int64)
    nextstart = trees_and_locations['Offset'][1:]
    thisend = nextstart - nexttidlen
    treenbytes[0:-1] = thisend - trees_and_locations['Offset'][0:-1]

    # Now fix the last tree for any file, (special-case the absolute last tree)
    uniq_tree_filenames = list(set(trees_and_locations['Filename']))
    filesizes_dict = {fname: os.stat(fname).st_size
                      for fname in uniq_tree_filenames}

    fileid_changes_ind = [ntrees-1]
    if len(uniq_tree_filenames) > 1:
        fileid = trees_and_locations['FileID'][:]
        fileid_diffs = np.diff(fileid)
        ind = (np.where(fileid_diffs != 0))[0]
        if ind:
            fileid_changes_ind.extend(ind)

    for ii in fileid_changes_ind:
        fname = trees_and_locations['Filename'][ii]
        off = trees_and_locations['Offset'][ii]
        treenbytes[ii] = filesizes_dict[fname] - off

    if treenbytes.min() <= 0:
        msg = "Error: Trees must span more than 0 bytes. "\
             f"Found treenbytes.min() = {treenbytes.min()}"
        raise AssertionError(msg)

    trees_and_locations['TreeNbytes'][:] = treenbytes
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: Computing number of bytes per tree...done. "
          f"Time taken = {t1-t0:.2f} seconds")

    # Now every tree has an associated size in bytes
    # Now let's re-sort the trees such that all trees from
    # the same forest are contiguous in the array.
    trees_and_locations.sort(order=['ForestID', 'Filename', 'Offset'],
                             kind='heapsort')

    return trees_and_locations


def get_aggregate_forest_info(trees_and_locations, rank=0):
    """
    Returns forest-level information from the tree-level information
    supplied.

    Parameters
    -----------

    trees_and_locations: numpy structured array, required
        A numpy structured array that contains the tree-level
        information. Should be the output from the function
        ``read_locations_and_forests``

    rank: integer, optional, default:0
        An integer identifying which task is calling this function. Only
        used in status messages

    Returns
    -------

    forest_info: A numpy structured array
        The structured array contains the fields
        ['ForestID', 'ForestNhalos', 'Input_ForestNbytes', 'Ntrees']
        The 'ForestNhalos' field is set to 0, and is populated as the
        trees are processed. The number of elements in the array is
        equal to the number of forests.

    """

    import numpy as np

    # The smallest granularity for parallelisation is a
    # single forest --> therefore, we also create this
    # additional array with info at the forest level.

    ntrees = trees_and_locations.shape[0]
    # The strategy requires the 'trees_and_locations' to be
    # already sorted on 'ForestID'
    uniq_forestids, ntrees_per_forest = \
        np.unique(trees_and_locations['ForestID'],
                  return_counts=True)
    nforests = uniq_forestids.shape[0]
    forest_info_dtype = np.dtype([('ForestID', np.int64),
                                  ('ForestNhalos', np.int64),
                                  ('Input_ForestNbytes', np.int64),
                                  ('Ntrees', np.int64)])

    forest_info = np.empty(nforests, dtype=forest_info_dtype)
    forest_info['ForestID'][:] = uniq_forestids

    # We can only fill in the number of halos *after* reading in
    # the halos (across all the trees)
    forest_info['ForestNhalos'][:] = 0
    forest_info['Ntrees'][:] = ntrees_per_forest

    t0 = time.perf_counter()
    print(f"[Rank={rank}]: Calculating number of bytes per forest...")
    ends = np.cumsum(ntrees_per_forest)
    if ends[-1] != ntrees:
        msg = "Error: Something strange happened while computing the "\
              "number of bytes per tree. Expected the last element "\
              "of the array containing the cumulative sum of number "\
             f"of trees per forest = '{ends[-1]}' to equal the total number "\
             f"of trees being processed = '{ntrees}'. Please check "\
              "that the TreeRootID and ForestID values are unique...exiting"
        raise AssertionError(msg)

    starts = np.zeros_like(ends, dtype=np.int64)
    starts[1:] = ends[0:-1]

    treenbytes = trees_and_locations['TreeNbytes'][:]
    if treenbytes.min() <= 0:
        msg = "Error: While computing the number of bytes per forest. "\
              "Expected *all* trees to contain at least one entry "\
              "(i.e., span non-zero bytes in the input file)."\
              "However, minimum of the array containing the "\
             f"number of bytes per tree = '{treenbytes.min()}'."\
              "Perhaps the input files are corrupt? Exiting..."
        raise AssertionError(msg)

    forestnbytes = np.array([treenbytes[start:end].sum()
                             for start, end in zip(starts, ends)])
    if forestnbytes.shape[0] != nforests:
        msg = "Error: There must be some logic issue in the code. Expected "\
              "that the shape of the array containing the number of bytes "\
             f"per forest = '{forestnbytes.shape[0]}' to be *exactly* "\
             f"equal to nforests = '{nforests}'. Exiting..."
        raise AssertionError(msg)

    # Since we have already checked that 'treenbytes.min() > 0'
    # --> no need to check that forestnbytes.min() is > 0

    forest_info['Input_ForestNbytes'][:] = forestnbytes
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: Calculating number of bytes per forest...done. "
          f"Time taken = {t1-t0:0.2f} seconds")

    return forest_info


def get_all_parallel_ctrees_filenames(fname):
    """
    Returns three filenames corresponding to the 'forests.list',
    'locations.dat', and 'tree_*.dat' files *assuming* the naming
    convention the Uchuu collaboration's parallel Consistent-Trees code

    Parameters
    -----------

    fname: string, required
        A filename specifying the tree data file for a parallel
        Consistent-Trees soutput

    Returns
    --------

    forests_file, locations_file, treedata_file: strings, filenames
        The filenames corresponding to the 'forests.list', 'locations.dat' and
        the 'tree_*.dat' file *generated using* the convention of the Uchuu
        colloboration. The convention is:
            'forests.list'  ---> '<prefix>.forest'
            'locations.dat' ---> '<prefix>.loc'
            'tree_*.dat'    ---> '<prefix>.tree'

    """
    import os

    fname = check_and_decompress(fname)

    # Need to create the three filenames for the
    # forests.list, locations.dat and tree*.dat files
    dirname = os.path.dirname(fname)

    # Get the filename and remove the file extension
    basename, _ = os.path.splitext(os.path.basename(fname))

    forests_file = f'{dirname}/{basename}.forest'
    locations_file = f'{dirname}/{basename}.loc'
    treedata_file = f'{dirname}{basename}.tree'

    return forests_file, locations_file, treedata_file


def check_forests_locations_filenames(filenames):
    """
    Accepts two filenames (in any order) and checks whether
    the files contain the correct data as expected
    in 'forests.list', 'locations.dat' and returns the
    files as (equivalent to) 'forests.list' and 'locations.dat'

    Parameters
    -----------

    filenames: list of two filenames, string, required
        List containing two filenames corresponding to the standard
        'forests.list' and 'locations.dat' (in any order, i.e.,
        both ['forests.list', 'locations.dat'] *and*
        ['locations.dat', 'forests.list'] are valid)

    Returns
    --------

    forests_file, locations_file: strings, filenames
        The filenames equivalent to the 'forests.list', 'locations.dat'
        (in that order)

    """
    if len(filenames) != 2:
        msg = "Error: Expected to get only two filenames. Instead got "\
              f"filenames = '{filenames}' with len(filenames) = "\
              f"{len(filenames)}"
        raise AssertionError(msg)

    forests_file, locations_file = filenames[0], filenames[1]

    # Check if the first line in the potential `forests_file` contains
    # the expected 'ForestID'. We could even check that the entire
    # first line matches '#TreeRootID ForestID'
    with generic_reader(forests_file, 'rt') as f:
        line = f.readline()
        if 'ForestID' not in line:
            forests_file, locations_file = locations_file, forests_file

    # Now check that the locations file is a valid CTrees file
    with generic_reader(locations_file, 'rt') as f:
        line = f.readline()
        if 'FileID' not in line or \
           'Offset' not in line or \
           'Filename' not in line:
            msg = f"Error: The first line in the locations_file = "\
                  f"'{locations_file}' does not contain the expected "\
                  f"fields. First line = '{line}'"
            raise AssertionError(msg)

    return forests_file, locations_file


def validate_inputs_are_ctrees_files(ctrees_filenames, base_metadata=None,
                                     base_version=None,
                                     base_input_catalog_type=None):
    """
    Checks the files contain Consistent-Trees catalogues derived from
    the same simulation.

    Parameters
    -----------

    ctrees_filenames: list of filenames, string, required
        The input filenames (potentially) containing Consistent-Trees
        catalogues.

        Note: Only unique filenames within this list are checked

    Returns
    --------

        The FofID field Returns ``True`` when all the files containing
        valid Consistent-Trees catalogues, *and* the same header info,
        i.e., same simulation + Consistent-Trees setup.
        Otherwise, a ``ValueError`` is raised.

    """
    import numpy as np

    # Read the entire header meta-data from the first file
    files = np.unique(ctrees_filenames)
    if not base_version:
        base_metadata_dict = get_metadata(files[0])
        base_metadata = np.string_(base_metadata_dict['metadata'])
        base_version = base_metadata_dict['version']
        base_input_catalog_type = base_metadata_dict['catalog_type']

    for fname in files:
        metadata_dict = get_metadata(fname)
        metadata = np.string_(metadata_dict['metadata'])
        version = metadata_dict['version']
        input_catalog_type = metadata_dict['catalog_type']

        if 'Consistent' not in input_catalog_type:
            msg = "Error: This script is meant *only* to process "\
                  "Consistent-Tree catalogs. Found catalog type = "\
                  f"{input_catalog_type} instead (input file = {fname})"
            raise ValueError(msg)

        if 'hlist' in input_catalog_type:
            msg = "Error: This script is meant *only* to process "\
                  "Consistent-Tree catalogs containing tree data. "\
                  "Found a halo catalogue (generated by "\
                  "Consistent-Trees)\" instead. catalog type = "\
                  f"{input_catalog_type} instead (input file = {fname})"
            raise ValueError(msg)

        if base_input_catalog_type != input_catalog_type:
            msg = f"Error: Catalog type = '{input_catalog_type}' for file "\
                  f"'{fname}' does *not* match the catalog type = "\
                  f"'{base_input_catalog_type}' for file '{files[0]}'"
            raise ValueError(msg)

        if base_version != version:
            msg = f"Error: Version = '{version}' for file '{fname}' does "\
                  f"*not* match the version = '{base_version}' for "\
                  f"file '{files[0]}'"
            raise ValueError(msg)

        if not np.array_equal(base_metadata, metadata):
            msg = f"Error: metadata = '{metadata}' for file '{fname}' "\
                  f"does *not* match the metadata = '{base_metadata}' "\
                  f"for file '{files[0]}'"
            raise ValueError(msg)

    return True


def assign_fofids(forest, rank=0):
    """
    Fills the FofID field for all halos.

    Parameters
    -----------

    forest: numpy structured array, required
        An array containing all halos from the same forest

        Note: The tree-walking indices (i.e., columns returned by
        the function ``get_treewalk_dtype_descr``) are expected to be
        filled with -1.

    rank: integer, optional, default=0
        The unique identifier for the current task. Only used
        within an error statement

    Returns
    --------
        The FofID field is filled in-place, and this function has
        no return value

    """

    import numpy as np

    # Assign fofhaloids to the halos
    fofhalos = (np.where(forest['pid'] == -1))[0]
    if len(fofhalos) == 0:
        nhalos = forest.shape[0]
        msg = f"Error: There are no FOF halos among these {nhalos} passed.\n"
        msg += f"forest = {forest}"
        raise ValueError(msg)

    # First fix the upid, and assign the (self) FofIDs
    # to the FOF halos
    fofhalo_ids = forest['id'][fofhalos]
    forest['upid'][fofhalos] = fofhalo_ids
    forest['FofID'][fofhalos] = fofhalo_ids

    # First set the upid, fofid for the FOFs
    haloids = forest['id'][:]
    sorted_halo_idx = np.argsort(haloids)

    rem_sub_inds = (np.where(forest['FofID'] == -1))[0]
    nleft = len(rem_sub_inds)
    while nleft > 0:
        # Need to match un-assigned (pid, upid) with fofid, id of halos with
        # assigned fofid
        nassigned = 0
        for fld in ['upid', 'pid']:
            if len(rem_sub_inds) != nleft:
                msg = "Error: (Bug in code) Expected to still have some "\
                      f"remaining subhalos that needed assigning, since "\
                      f"nleft = {nleft} "\
                      f"with len(rem_sub_inds) = {len(rem_sub_inds)}\n"\
                      f"rem_sub_inds = {rem_sub_inds}."
                raise AssertionError(msg)

            fld_id = forest[fld][rem_sub_inds]

            # 'pid'/'upid' might be duplicated ->
            # np.intersect1d will only return one value; other
            # halos with the same 'pid' will not get updated to the
            # correct fof. Need a 'searchsorted' like implementation
            # but know that not *all* pids will be found
            fld_idx = np.searchsorted(haloids, fld_id, sorter=sorted_halo_idx)
            forest['FofID'][rem_sub_inds] = forest['FofID'][sorted_halo_idx[fld_idx]]
            nassigned += len(rem_sub_inds)
            rem_sub_inds = np.where(forest['FofID'] == -1)[0]
            nassigned -= len(rem_sub_inds)
            msg = f"Error: nassigned = {nassigned} must be at least 0."
            assert nassigned >= 0, msg
            nleft = len(rem_sub_inds)
            if nleft == 0:
                break

        if nleft > 0 and nassigned == 0:
            msg = f"[Rank={rank}]: Error: There are {nleft} halos left "\
                  f"without fofhalos assigned but could not assign a "\
                  f"single fof halo in this iteration\n"
            xx = np.where(forest['FofID'] == -1)[0]
            msg += f"forest['pid'][xx] = {forest['pid'][xx]}\n"
            msg += f"forest['upid'][xx] = {forest['upid'][xx]}\n"
            msg += f"forest[xx] = {forest[xx]}\n"
            pid = forest['pid'][xx]
            xx = np.where(forest['id'] == pid)[0]
            msg += f"pid = {pid} forest['id'] = {forest['id'][xx]} "\
                   f"(should be equal to 'pid')\n"
            msg += f"fofid for the 'pid' halo = {forest['FofID'][xx]}"
            raise ValueError(msg)

    # Reset the FOF upid field
    assert forest['pid'][fofhalos].min() == -1
    assert forest['pid'][fofhalos].max() == -1
    forest['upid'][fofhalos] = -1

    return


def get_treewalk_dtype_descr():
    """
    Returns the description for the additional fields
    to add to the forest for walking the mergertree

    Parameters
    -----------
    None

    Returns
    --------
    mergertree_descr: list of tuples
        A list of tuples containing the names and datatypes
        for the additional columns needed for walking the
        mergertree. This list can be used to create a numpy
        datatype suitable to contain the additional mergertree
        indices

    """

    import numpy as np
    mergertree_descr = [('FofID', np.int64),
                        ('FirstHaloInFOFgroup', np.int64),
                        ('NextHaloInFOFgroup', np.int64),
                        ('PrevHaloInFOFgroup', np.int64),
                        ('FirstProgenitor', np.int64),
                        ('NextProgenitor', np.int64),
                        ('PrevProgenitor', np.int64),
                        ('Descendant', np.int64)]

    return mergertree_descr


def add_tree_walk_indices(forest, rank=0):
    """
    Adds the various mergertree walking indices

    Parameters
    -----------

    forest: numpy structured array, required
        An array containing all halos from the same forest

    rank: integer, optional, default=0
        The unique identifier for the current task. Only used
        within an error statement

    Returns
    --------

    None
        The mergertree indices are filled in-place and no additional
        return occurs

    """
    import numpy as np

    mergertree_descr = get_treewalk_dtype_descr()

    # initialise all the mergertree indices
    for name, _ in mergertree_descr:
        forest[name][:] = -1

    # Assign the FOFhalo IDs
    assign_fofids(forest, rank)

    # Assign the index for the fofhalo
    forest['Mvir'] *= -1  # hack so that the sort is decreasing order
    order = ['scale', 'FofID', 'upid', 'Mvir', 'id']
    sorted_fof_order = forest.argsort(order=order)
    forest['Mvir'] *= -1  # reset mass back

    # upid of FOFs is set to -1, therefore the FOF halo should be the first
    # halo with that FOFID (i.e., equal to its own haloID)
    uniq_fofs, firstfof_inds = np.unique(forest['FofID'][sorted_fof_order],
                                         return_index=True)

    nextsub_loc = np.split(sorted_fof_order, firstfof_inds[1:])
    for lhs in nextsub_loc:
        assert forest['FofID'][lhs].min() == forest['FofID'][lhs].max()
        assert forest['FofID'][lhs].min() == forest['id'][lhs[0]]
        forest['FirstHaloInFOFgroup'][lhs] = lhs[0]
        forest['NextHaloInFOFgroup'][lhs] = [*lhs[1:], -1]
        forest['PrevHaloInFOFgroup'][lhs] = [-1, *lhs[:-1]]

    # Now match the descendants
    haloids = forest['id'][:]
    desc_ids = forest['desc_id'][:]
    sorted_halo_idx = np.argsort(haloids)

    valid_desc_idx = desc_ids != -1
    desc_ind = np.searchsorted(haloids, desc_ids[valid_desc_idx],
                               sorter=sorted_halo_idx)

    msg = "np.searchsorted did not work as expected"
    np.testing.assert_array_equal(desc_ids[valid_desc_idx],
                                  haloids[sorted_halo_idx[desc_ind]],
                                  err_msg=msg)
    forest['Descendant'][valid_desc_idx] = sorted_halo_idx[desc_ind]

    # Check that *all* desc_ids were found
    msg = "Expected to find *all* descendants"
    assert set(haloids[sorted_halo_idx[desc_ind]]) == set(desc_ids[valid_desc_idx]), msg

    np.testing.assert_array_equal(forest['desc_id'][valid_desc_idx],
                                  forest['id'][sorted_halo_idx[desc_ind]],
                                  err_msg='descendant ids are incorrect')
    np.testing.assert_array_equal(forest['desc_scale'][valid_desc_idx],
                                  forest['scale'][sorted_halo_idx[desc_ind]],
                                  err_msg='descendant scales are incorrect')

    # Now assign (firstprog, nextprog)
    # Sort on the descendants and then mass, but only get the index
    # This is confusing, but really what the sorted index refers to are the
    # progenitors and this is the order in which the progenitors should be
    # assigned as FirstProg, NextProg
    forest['Mvir'] *= -1  # hack so that the sort is decreasing order
    sorted_prog_inds = np.argsort(forest, order=['desc_id', 'Mvir'])
    forest['Mvir'] *= -1  # reset the masses back

    # t0 = time.perf_counter()
    valid_desc_idx = np.where(forest['desc_id'][sorted_prog_inds] != -1)[0]
    uniq_descid, firstprog_inds = \
        np.unique(forest['desc_id'][sorted_prog_inds[valid_desc_idx]],
                  return_index=True)
    desc_for_firstprog_idx = np.searchsorted(haloids, uniq_descid,
                                             sorter=sorted_halo_idx)

    # np.testing.assert_array_equal(firstprog, forest['FirstProgenitor'])
    forest['FirstProgenitor'][sorted_halo_idx[desc_for_firstprog_idx]] = \
        sorted_prog_inds[valid_desc_idx[firstprog_inds]]

    # Check that the firstprog assignment was okay
    xx = np.where(forest['FirstProgenitor'] != -1)[0]
    firstprog = forest['FirstProgenitor'][xx]
    np.testing.assert_array_equal(forest['desc_id'][firstprog],
                                  forest['id'][xx])

    # See https://stackoverflow.com/a/53507580 and
    # https://stackoverflow.com/a/54736464
    valid_prog_loc = np.split(sorted_prog_inds[valid_desc_idx],
                              firstprog_inds[1:])
    for lhs in valid_prog_loc:
        assert forest['desc_id'][lhs].min() == forest['desc_id'][lhs].max()
        desc = forest['Descendant'][lhs[0]]
        assert forest['FirstProgenitor'][desc] == lhs[0]
        mvir = forest['Mvir'][lhs]
        assert np.all(np.diff(mvir) <= 0.0)
        forest['NextProgenitor'][lhs] = [*lhs[1:], -1]
        forest['PrevProgenitor'][lhs] = [-1, *lhs[0:-1]]

    return
