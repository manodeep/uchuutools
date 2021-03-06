#!/usr/bin/env python
from __future__ import print_function

__author__ = "Manodeep Sinha"
__all__ = ["convert_halocat_to_h5"]

import os

from ..utils import get_parser, get_approx_totnumhalos, generic_reader,\
                    get_metadata, resize_halo_datasets, write_halos


def _convert_single_halocat(input_file, rank,
                            outputdir, write_halo_props_cont,
                            fields, drop_fields,
                            chunksize, compression,
                            show_progressbar):
    """
    Convert a single Rockstar/Consistent Trees (an hlist catalogue) file
    into an (optionally compressed) hdf5 file.

    Parameters
    -----------

    input_file: string, required
        The input filename for the Rockstar/Consistent Trees file. Can
        be a compressed (.gz, .bz2) file.

    rank: integer, required
        The (MPI) rank for the process. The output filename is determined
        with this rank to ensure unique filenames when running in parallel.

    outputdir: string, required
        The directory where the converted hdf5 file will be written in. The
        output filename is obtained by appending '.h5' to the ``input_file``.
        If the output file already exists, then it will be truncated.

    write_halo_props_cont: boolean, required
        Controls if the individual halo properties are written as distinct
        datasets such that any given property for ALL halos is written
        contiguously (structure of arrays, SOA).

        When set to False, only one dataset ('halos') is created, and ALL
        properties of a halo is written out contiguously (array of
        structures).

        In both cases, the halos are written under the root group ``HaloCatalogue``.

    fields: list of strings, required
        Describes which specific columns in the input file to carry across
        to the hdf5 file. Default action is to convert ALL columns.

    drop_fields: list of strings, required
        Describes which columns are not carried through to the hdf5 file.
        Processed after ``fields``, i.e., you can specify ``fields=None`` to
        create an initial list of *all* columns in the ascii file, and then
        specify ``drop_fields = [colname2, colname7, ...]``, and those columns
        will not be present in the hdf5 output.

    chunksize: integer, required
        Controls how many lines are read in from the input file before being
        written out to the hdf5 file.

    compression: string, required
        Controls the kind of compression applied. Valid options are anything
        that ``h5py`` accepts.

    show_progressbar: boolean, required
        Controls whether a progressbar is printed. Only enables progressbar
        on rank==0, the remaining ranks ignore this keyword.

    Returns
    -------

        Returns ``True`` on successful completion.

    """

    import numpy as np
    import h5py
    import time
    import sys
    import pandas as pd
    from tqdm import tqdm

    if rank != 0:
        show_progressbar = False

    if not os.path.isdir(outputdir):
        msg = f"Error: The first parameter (output directory) = "\
              f"'{outputdir}' should be of type directory"
        raise ValueError(msg)

    if chunksize < 1:
        print(f"Warning: chunksize (the number of lines read in one "
              f"shot = '{chunksize}' must be at least 1")
        raise ValueError(msg)

    print(f"[Rank={rank}]: processing file '{input_file}'...")
    sys.stdout.flush()
    t0 = time.perf_counter()

    # Read the entire header meta-data
    metadata_dict = get_metadata(input_file)
    metadata = metadata_dict['metadata']
    version_info = metadata_dict['version']
    input_catalog_type = metadata_dict['catalog_type']
    hdrline = metadata_dict['headerline']

    # Check that this is not a Consistent-Tree "tree_*.dat" file
    if ('Consistent' in input_catalog_type) and \
       ('hlist' not in input_catalog_type):
        msg = f"Error: This script can *only* convert the 'hlist' halo "\
              f"catalogues generated by Consistent-Trees. Seems like a "\
              f"Consistent-Tree generated tree (instead of 'hlist') "\
              f"catalogue was present the file = '{input_file}' "\
              f"supplied...exiting"
        raise ValueError(msg)

    parser = get_parser(input_file, fields=fields, drop_fields=drop_fields)

    approx_totnumhalos = get_approx_totnumhalos(input_file)
    if show_progressbar:
        pbar = tqdm(total=approx_totnumhalos, unit=' halos', disable=None)

    halos_offset = 0
    input_filebase = os.path.basename(input_file)
    output_file = f"{outputdir}/{input_filebase}.h5"

    with h5py.File(output_file, "w") as hf:
        line_with_scale_factor = ([line for line in metadata
                                   if line.startswith("#a")])[0]
        scale_factor = float((line_with_scale_factor.split('='))[1])
        redshift = 1.0/scale_factor - 1.0

        # give the HDF5 root some attributes
        hf.attrs[u"input_filename"] = np.string_(input_file)
        hf.attrs[u"input_filedatestamp"] = np.array(os.path.getmtime(input_file))
        hf.attrs[u"input_catalog_type"] = np.string_(input_catalog_type)
        hf.attrs[f"{input_catalog_type}_version"] = np.string_(version_info)
        hf.attrs[f"{input_catalog_type}_columns"] = np.string_(hdrline)
        hf.attrs[f"{input_catalog_type}_metadata"] = np.string_(metadata)
        sim_grp = hf.create_group('simulation_params')
        simulation_params = metadata_dict['simulation_params']
        for k, v in simulation_params.items():
            sim_grp.attrs[f"{k}"] = v

        hf.attrs[u"HDF5_version"] = np.string_(h5py.version.hdf5_version)
        hf.attrs[u"h5py_version"] = np.string_(h5py.version.version)
        hf.attrs[u"TotNhalos"] = -1
        hf.attrs[u"scale_factor"] = scale_factor
        hf.attrs[u"redshift"] = redshift

        halos_grp = hf.create_group('HaloCatalogue')
        halos_grp.attrs['scale_factor'] = scale_factor
        halos_grp.attrs['redshift'] = redshift

        dset_size = approx_totnumhalos
        if write_halo_props_cont:
            halos_dset = dict()
            # Create a dataset for every halo property
            # For any given halo property, the value
            # for halos will be written contiguously
            # (structure of arrays)
            for name, dtype in parser.dtype.descr:
                halos_dset[name] = \
                    halos_grp.create_dataset(name,
                                             (dset_size, ),
                                             dtype=dtype,
                                             chunks=True,
                                             compression=compression,
                                             maxshape=(None,))
        else:
            # Create a single dataset that contains all properties
            # of a given halo, then all properties of the next halo,
            # and so on (array of structures)
            halos_dset = halos_grp.create_dataset('halos', (dset_size,),
                                                  dtype=parser.dtype,
                                                  chunks=True,
                                                  compression=compression,
                                                  maxshape=(None,))

        # Open the file with the generic reader ('inp' will be
        # a generator). Compressed files are also fine
        with generic_reader(input_file, 'rt') as inp:

            # Create a generator with pandas ->
            # the generator will yield 'chunksize' lines at a time
            gen = pd.read_csv(inp, dtype=parser.dtype, memory_map=True,
                              names=parser.dtype.names, delim_whitespace=True,
                              index_col=False, chunksize=chunksize,
                              comment='#')
            for chunk in gen:
                # convert to a rec-array
                halos = chunk.to_records(index=False)

                # convert to structured nd-array
                halos = np.asarray(halos, dtype=parser.dtype)

                nhalos = halos.shape[0]
                if (halos_offset + nhalos) > dset_size:
                    resize_halo_datasets(halos_dset, halos_offset + nhalos,
                                         write_halo_props_cont, parser.dtype)
                    dset_size = halos_offset + nhalos

                write_halos(halos_dset, halos_offset, halos, nhalos,
                            write_halo_props_cont)
                halos_offset += nhalos

                if show_progressbar:
                    # Since the total number of halos being
                    # processed is approximate -- let's
                    # update the progressbar total to the
                    # best guess at the moment
                    pbar.total = dset_size
                    pbar.update(nhalos)

        # The ascii file has now been read in entirely -> Now fix the actual
        # dataset sizes to reflect the total number of halos written
        resize_halo_datasets(halos_dset, halos_offset,
                             write_halo_props_cont, parser.dtype)
        dset_size = halos_offset

        hf.attrs['TotNhalos'] = halos_offset
        if show_progressbar:
            pbar.close()

    totnumhalos = halos_offset
    t1 = time.perf_counter()
    print(f"[Rank={rank}]: processing file {input_file}.....done. "
          f"Wrote {totnumhalos} halos in {t1-t0:.2f} seconds")
    sys.stdout.flush()
    return True


def convert_halocat_to_h5(filenames, outputdir="./",
                          write_halo_props_cont=True,
                          fields=None, drop_fields=None,
                          chunksize=100000, compression='gzip',
                          comm=None, show_progressbar=False):

    """
    Converts a list of Rockstar/Consistent-Trees halo catalogues from
    ascii to hdf5.

    Can be used with MPI but requires that the number of files to be larger
    than the number of MPI tasks spawned.

    Parameters
    -----------

    filenames: list of strings, required
        A list of filename(s) for the Rockstar/Consistent Trees file. Can
        be compressed (.gz, .bz2, .xz, .zip) files.

    outputdir: string, optional, default: current working directory ('./')
        The directory where the converted hdf5 file will be written in. The
        output filename is obtained by appending '.h5' to the ``input_file``.
        If the output file already exists, then it will be truncated.

    write_halo_props_cont: boolean, optional, default: True
        Controls if the individual halo properties are written as distinct
        datasets such that any given property for ALL halos is written
        contiguously (structure of arrays, SOA).

        When set to False, only one dataset ('halos') is created, and ALL
        properties of a halo is written out contiguously (array of
        structures).

    fields: list of strings, optional, default: None
        Describes which specific columns in the input file to carry across
        to the hdf5 file. Default action is to convert ALL columns.

    drop_fields: list of strings, optional, default: None
        Describes which columns are not carried through to the hdf5 file.
        Processed after ``fields``, i.e., you can specify ``fields=None`` to
        create an initial list of *all* columns in the ascii file, and then
        specify ``drop_fields = [colname2, colname7, ...]``, and those columns
        will not be present in the hdf5 output.

    chunksize: integer, optional, default: 100000
        Controls how many lines are read in from the input file before being
        written out to the hdf5 file.

    compression: string, optional, default: 'gzip'
        Controls the kind of compression applied. Valid options are anything
        that ``h5py`` accepts.

    comm: MPI communicator, optional, default: None
        Controls whether the conversion is run in MPI parallel. Should be
        compatible with `mpi4py.MPI.COMM_WORLD`.

    show_progressbar: boolean, optional, default: False
        Controls whether a progressbar is printed. Only enables progressbar
        on rank==0, the remaining ranks ignore this keyword.

    Returns
    -------
        Returns ``True`` on successful completion.

    """
    import sys
    import time

    rank = 0
    ntasks = 1
    if comm:
        rank = comm.Get_rank()
        ntasks = comm.Get_size()

    sys.stdout.flush()
    nfiles = len(filenames)
    if nfiles < ntasks:
        msg = f"Nfiles = {nfiles} must be >= the number of tasks = {ntasks}"
        raise ValueError(msg)

    tstart = time.perf_counter()
    if rank == 0:
        print(f"[Rank={rank}]: Converting nfiles = {nfiles} over ntasks = "
              f"{ntasks}...")

    # Convert files in MPI parallel (if requested)
    # the range will produce filenum starting with "rank"
    # and then incrementing by "ntasks" all the way upto
    # and inclusive of [nfiles-1]. That is, the range [0, nfiles-1]
    # will be uniquely distributed over ntasks.
    for filenum in range(rank, nfiles, ntasks):
        _convert_single_halocat(filenames[filenum], rank,
                                outputdir=outputdir,
                                write_halo_props_cont=write_halo_props_cont,
                                fields=fields,
                                drop_fields=drop_fields,
                                chunksize=chunksize,
                                compression=compression,
                                show_progressbar=show_progressbar)

    # The barrier is only essential so that the total time printed
    # out on rank==0 is correct.
    if comm:
        comm.Barrier()

    if rank == 0:
        t1 = time.perf_counter()
        print(f"[Rank={rank}]: Converting nfiles = {nfiles} over ntasks = "
              f"{ntasks}...done. Time taken = {t1-tstart:0.2f} seconds")

    return True
