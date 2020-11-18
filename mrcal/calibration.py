#!/usr/bin/python3

'''Helper routines for seeding and solving camera calibration problems

All functions are exported into the mrcal module. So you can call these via
mrcal.calibration.fff() or mrcal.fff(). The latter is preferred.

'''

import numpy as np
import numpysane as nps
import sys
import re
import cv2
import mrcal

def compute_chessboard_corners(Nw, Nh, globs=('*',), corners_cache_vnl=None, jobs=1,
                               exclude_images=set(),
                               weighted=True,
                               keep_level=False):
    r'''Compute the chessboard observations and returns them in a usable form

SYNOPSIS

    observations, indices_frame_camera, paths = \
        mrcal.compute_chessboard_corners(10, 10,
                                         ('frame*-cam0.jpg','frame*-cam1.jpg'),
                                         "corners.vnl")

The input to a calibration problem is a set of images of a calibration object
from different angles and positions. This function ingests these images, and
outputs the detected chessboard corner coordinates in a form usable by the mrcal
optimization routines. The "corners_cache_vnl" argument specifies a file to read
from, or write to. This is a vnlog with legend

    # filename x y level

Each record is a chessboard corner. The image filename and corner coordinates
are given. The "level" is a decimation level of the detected corner. If we
needed to cut down the image resolution to detect a corner, its coordinates are
known less precisely, and we use that information to weight the errors
appropriately later. We are able to read data that is missing the "level" field:
a level of 0 (weight = 1) will be filled in. If we read data from a file,
records with a "-" level mean "skip this point". We'll report the point with
weight < 0. Any images with fewer than 3 points will be ignored entirely.

if keep_level: then instead of converting a decimation level to a weight, we'll
just write the decimation level into the returned observations array. We write
<0 to mean "skip this point".

ARGUMENTS

- Nw, Nh: the width and height of the point grid in the calibration object we're
  using

- globs: a list of strings, one per camera, containing globs matching the image
  filenames for that camera. The filenames are expected to encode the
  instantaneous frame numbers, with identical frame numbers implying
  synchronized images. A common scheme is to name an image taken by frame C at
  time T "frameT-camC.jpg". Then images frame10-cam0.jpg and frame10-cam1.jpg
  are assumed to have been captured at the same moment in time by cameras 0 and
  1. With this scheme, if you wanted to calibrate these two cameras together,
  you'd pass ('frame*-cam0.jpg','frame*-cam1.jpg') in the "globs" argument.

  The "globs" argument may be omitted. In this case all images are mapped to the
  same camera.

- corners_cache_vnl: the name of a file to use to read/write the detected
  corners; or a python file object to read data from. If the given file exists
  or a python file object is given, we read the detections from it, and do not
  run the detector. If the given file does NOT exist (which is what happens the
  first time), mrgingham will be invoked to compute the corners from the images,
  and the results will be written to that file. So the same function call can be
  used to both compute the corners initially, and to reuse the pre-computed
  corners with subsequent calls. This exists to save time where re-analyzing the
  same data multiple times.

- jobs: a GNU-Make style parallelization flag. Indicates how many parallel
  processes should be invoked when computing the corners. If given, a numerical
  argument is required. This flag does nothing if the corners-cache file already
  exists

- exclude_images: a set of filenames to exclude from reported results

- weighted: corner detectors can report an uncertainty in the coordinates of
  each corner, and we use that by default. To ignore this, and to weigh all the
  corners equally, call with weighted=True

- keep_level: if True, we write the decimation level into the observations
  array, instead of converting it to a weight first. If keep_level: then
  "weighted" has no effect. The default is False.

RETURNED VALUES

This function returns a tuple (observations, indices_frame_camera, files_sorted)

- observations: an ordered (N,object_height_n,object_width_n,3) array describing
  N board observations where the board has dimensions
  (object_height_n,object_width_n) and each point is an (x,y,weight) pixel
  observation. A weight<0 means "ignore this point". Incomplete chessboard
  observations can be specified in this way. if keep_level: then the decimation
  level appears in the last column, instead of the weight

- indices_frame_camera is an (N,2) array of contiguous, sorted integers where
  each observation is (index_frame,index_camera)

- files_sorted is a list of paths of images corresponding to the observations

Note that this assumes we're solving a calibration problem (stationary cameras)
observing a moving object, so this returns indices_frame_camera. It is the
caller's job to convert this into indices_frame_camintrinsics_camextrinsics,
which mrcal.optimize() expects

    '''

    import os
    import fnmatch
    import subprocess
    import shutil
    from tempfile import mkstemp
    import io
    import copy

    def get_corner_observations(Nw, Nh, globs, corners_cache_vnl, exclude_images=set()):
        r'''Return dot observations, from a cache or from mrgingham

        Returns a dict mapping from filename to a numpy array with a full grid
        of dot observations. If no grid was observed in a particular image, the
        relevant dict entry is empty

        The corners_cache_vnl argument is for caching corner-finder results.
        This can be None if we want to ignore this. Otherwise, this is treated
        as a path to a file on disk or a python file object. If this file
        exists:

            The corner coordinates are read from this file instead of being
            computed. We don't need to actually have the images stored on disk.
            Any image filenames mentioned in this cache file are matched against
            the globs to decide which camera the image belongs to. If it matches
            none of the globs, that image filename is silently ignored

        If this file does not exist:

            We process the images to compute the corner coordinates. Before we
            compute the calibration off these coordinates, we create the cache
            file and store this data there. Thus a subsequent identical
            invocation of mrcal-calibrate-cameras will see this file as
            existing, and will automatically use the data it contains instead of
            recomputing the corner coordinates

        '''

        # Expand any ./ and // etc
        globs = [os.path.normpath(g) for g in globs]

        Ncameras = len(globs)
        files_per_camera = []
        for i in range(Ncameras):
            files_per_camera.append([])

        # images in corners_cache_vnl have paths relative to where the
        # corners_cache_vnl lives
        corners_dir = None

        reading_pipe = isinstance(corners_cache_vnl, io.IOBase)

        if corners_cache_vnl is not None and not reading_pipe:
            corners_dir = os.path.dirname( corners_cache_vnl )

        def accum_files(f):
            for icam in range(Ncameras):
                g = globs[icam]
                if g[0] != '/':
                    g = '*/' + g
                if fnmatch.fnmatch(os.path.abspath(f), g):
                    files_per_camera[icam].append(f)
                    return True
            return False


        pipe_corners_write_fd          = None
        pipe_corners_write_tmpfilename = None
        if corners_cache_vnl is not None and \
           not reading_pipe              and \
           os.path.isdir(corners_cache_vnl):
            raise Exception("Given cache path '{}' is a directory. Must be a file or must not exist". \
                            format(corners_cache_vnl))

        if corners_cache_vnl is None or \
           ( not reading_pipe and \
             not os.path.isfile(corners_cache_vnl) ):
            # Need to compute the dot coords. And maybe need to save them into a
            # cache file too
            if Nw != 10 or Nh != 10:
                raise Exception("mrgingham currently accepts ONLY 10x10 grids")

            args_mrgingham = ['mrgingham', '--jobs',
                              str(jobs)]
            args_mrgingham.extend(globs)

            sys.stderr.write("Computing chessboard corners by running:\n   {}\n". \
                             format(' '.join(shellquote(s) for s in args_mrgingham)))
            if corners_cache_vnl is not None:
                # need to save the corners into a cache. I want to do this
                # atomically: if the dot-finding is interrupted I don't want to
                # be writing incomplete results, so I write to a temporary file
                # and then rename when done
                pipe_corners_write_fd,pipe_corners_write_tmpfilename = mkstemp('.vnl')
                sys.stderr.write("Will save corners to '{}'\n".format(corners_cache_vnl))

            corners_output = subprocess.Popen(args_mrgingham, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              encoding='ascii')
            pipe_corners_read = corners_output.stdout
        else:
            # Have an existing cache file. Just read it
            if reading_pipe:
                pipe_corners_read = corners_cache_vnl
            else:
                pipe_corners_read = open(corners_cache_vnl, 'r', encoding='ascii')
            corners_output    = None


        mapping = {}
        context0 = dict(f            = '',
                        igrid        = 0,
                        Nvalidpoints = 0)
        # The default weight is 1; the default decimation level is 0
        if keep_level: context0['grid'] = np.zeros((Nh*Nw,3), dtype=float)
        else:          context0['grid'] = np.ones( (Nh*Nw,3), dtype=float)

        context = copy.deepcopy(context0)

        def finish_chessboard_observation():
            nonlocal context
            if context['igrid']:
                if Nw*Nh != context['igrid']:
                    raise Exception("File '{}' expected to have {} points, but got {}". \
                                    format(context['f'], Nw*Nh, context['igrid']))
                if context['f'] not in exclude_images and \
                   context['Nvalidpoints'] > 3:
                    # There is a bit of ambiguity here. The image path stored in
                    # the 'corners_cache_vnl' file is relative to what? It could be
                    # relative to the directory the corners_cache_vnl lives in, or it
                    # could be relative to the current directory. The image
                    # doesn't necessarily need to exist. I implement a few
                    # heuristics to figure this out
                    if corners_dir is None          or \
                       context['f'][0] == '/'       or \
                       os.path.isfile(context['f']):
                        filename_canonical = os.path.normpath(context['f'])
                    else:
                        filename_canonical = os.path.join(corners_dir, context['f'])
                    if accum_files(filename_canonical):
                        mapping[filename_canonical] = context['grid'].reshape(Nh,Nw,3)
                context = copy.deepcopy(context0)

        for line in pipe_corners_read:
            if pipe_corners_write_fd is not None:
                os.write(pipe_corners_write_fd, line.encode())

            if line[0] == '#':
                continue
            m = re.match('(\S+)\s+(.*?)$', line)
            if m is None:
                raise Exception("Unexpected line in the corners output: '{}'".format(line))
            if m.group(2)[:2] == '- ':
                # No observations for this image. Done with this image; move on
                finish_chessboard_observation()
                continue
            if context['f'] != m.group(1):
                # Got data for the next image. Finish out this one
                finish_chessboard_observation()
                context['f'] = m.group(1)

            # The row may have 2 or 3 values: if 3, it contains a decimation
            # level of the corner observation (used for the weight). If 2, a
            # weight of 1.0 is assumed. The weight array is pre-filled with 1.0.
            # A decimation level of - will be used to set weight <0 which means
            # "ignore this point"
            fields = m.group(2).split()
            if len(fields) < 2:
                raise Exception("'corners.vnl' data rows must contain a filename and 2 or 3 values. Instead got line '{}'".format(line))
            else:
                context['grid'][context['igrid'],:2] = (float(fields[0]),float(fields[1]))
                context['Nvalidpoints'] += 1
                if len(fields) == 3:
                    if fields[2] == '-':
                        # ignore this point
                        context['grid'][context['igrid'],2] = -1.0
                        context['Nvalidpoints'] -= 1
                    elif keep_level:
                        context['grid'][context['igrid'],2] = float(fields[2])
                    elif weighted:
                        # convert decimation level to weight. The weight is
                        # 2^(-level). I.e. level-0 -> weight=1, level-1 ->
                        # weight=0.5, etc
                        context['grid'][context['igrid'],2] = 1. / (1 << int(fields[2]))
                    # else use the 1.0 that's already there

            context['igrid'] += 1

        finish_chessboard_observation()

        if corners_output is not None:
            sys.stderr.write("Done computing chessboard corners\n")

            if corners_output.wait() != 0:
                err = corners_output.stderr.read()
                raise Exception("mrgingham failed: {}".format(err))
            if pipe_corners_write_fd is not None:
                os.close(pipe_corners_write_fd)
                shutil.move(pipe_corners_write_tmpfilename, corners_cache_vnl)
        elif not reading_pipe:
            pipe_corners_read.close()

        # If I have multiple cameras, I use the filenames to figure out what
        # indexes the frame and what indexes the camera, so I need at least
        # two images for each camera to figure that out. Example:
        #
        #   I have two cameras, with one image each:
        #   - frame2-cam0.jpg
        #   - frame3-cam1.jpg
        #
        # If this is all I had, it'd be impossible for me to tell whether
        # the images correspond to the same frame or not. But if cam0 also
        # had "frame4-cam0.jpg" then I could look at the same-camera cam0
        # filenames, find the common prefixes,suffixes, and conclude that
        # the frame indices are 2 and 4.
        #
        # If I only have one camera, however, then the details of the
        # filenames don't matter, and I just make sure I have at least one
        # image to look at
        min_num_images = 2 if len(files_per_camera) > 1 else 1
        for icam in range(len(files_per_camera)):
            N = len(files_per_camera[icam])

            if N < min_num_images:
                raise Exception("Found too few ({}; need at least {}) images containing a calibration pattern in camera {}; glob '{}'". \
                                format(N, min_num_images, icam, globs[icam]))

        return mapping,files_per_camera


    indices_frame_camera = np.array((), dtype=np.int32)
    observations         = np.array((), dtype=float)

    # basic logic is this:
    #   for frames:
    #       for cameras:
    #           if have observation:
    #               push observations
    #               push indices_frame_camera
    mapping_file_corners,files_per_camera = get_corner_observations(Nw, Nh, globs, corners_cache_vnl, exclude_images)
    file_framenocameraindex               = mrcal.mapping_file_framenocameraindex(*files_per_camera)

    # I create a file list sorted by frame and then camera. So my for(frames)
    # {for(cameras) {}} loop will just end up looking at these files in order
    files_sorted = sorted(mapping_file_corners.keys(), key=lambda f: file_framenocameraindex[f][1])
    files_sorted = sorted(files_sorted,                key=lambda f: file_framenocameraindex[f][0])

    i_observation = 0

    iframe_last = None
    index_frame  = -1
    for f in files_sorted:
        # The frame indices I return are consecutive starting from 0, NOT the
        # original frame numbers
        iframe,icam = file_framenocameraindex[f]
        if iframe_last == None or iframe_last != iframe:
            index_frame += 1
            iframe_last = iframe

        indices_frame_camera = nps.glue(indices_frame_camera,
                                        np.array((index_frame, icam), dtype=np.int32),
                                        axis=-2)
        observations = nps.glue(observations,
                                mapping_file_corners[f],
                                axis=-4)

    return observations, indices_frame_camera, files_sorted


def estimate_monocular_calobject_poses_Rt_tocam( indices_frame_camera,
                                                 observations,
                                                 object_spacing,
                                                 models_or_intrinsics ):
    r"""Estimate camera-referenced poses of the calibration object from monocular views

SYNOPSIS

    print( indices_frame_camera.shape )
    ===>
    (123, 2)

    print( observations.shape )
    ===>
    (123, 3)

    models = [mrcal.cameramodel(f) for f in ("cam0.cameramodel",
                                             "cam1.cameramodel")]

    # Estimated poses of the calibration object from monocular observations
    Rt_camera_frame = \
        mrcal.estimate_monocular_calobject_poses_Rt_tocam( indices_frame_camera,
                                                           observations,
                                                           object_spacing,
                                                           models)

    print( Rt_camera_frame.shape )
    ===>
    (123, 4, 3)

    i_observation = 10
    icam = indices_frame_camera[i_observation,1]

    # The calibration object in its reference coordinate system
    calobject = mrcal.ref_calibration_object(object_width_n,
                                                 object_height_n,
                                                 object_spacing)

    # The estimated calibration object points in the observing camera coordinate
    # system
    pcam = mrcal.transform_point_Rt( Rt_camera_frame[i_observation],
                                     calobject )

    # The pixel observations we would see if the calibration object pose was
    # where it was estimated to be
    q = mrcal.project(pcam, *models[icam].intrinsics())

    # The reprojection error, comparing these hypothesis pixel observations from
    # what we actually observed. We estimated the calibration object pose from
    # the observations, so this should be small
    err = q - observations[i_observation][:2]

    print( np.linalg.norm(err) )
    ===>
    [something small]

mrcal solves camera calibration problems by iteratively optimizing a nonlinear
least squares problem to bring the pixel observation predictions in line with
actual pixel observations. This requires an initial "seed", an initial estimate
of the solution. This function is a part of that computation. Since this is just
an initial estimate that will be refined, the results of this function do not
need to be exact.

We have pixel observations of a known calibration object, and we want to
estimate the pose of this object in the coordinate system of the camera that
produced these observations. This function ingests a number of such
observations, and solves this "PnP problem" separately for each one. The
observations may come from any lens model; everything is reprojected to a
pinhole model first. This function is a wrapper around the solvePnP() openCV
call, which does all the work.

ARGUMENTS

- indices_frame_camera: an array of shape (Nobservations,2) and dtype
  numpy.int32. Each row (iframe,icam) represents an observation of a
  calibration object by camera icam. iframe is not used by this function

- observations: an array of shape
  (Nobservations,object_height_n,object_width_n,3). Each observation corresponds
  to a row in indices_frame_camera, and contains a row of shape (3,) for each
  point in the calibration object. Each row is (x,y,weight) where x,y are the
  observed pixel coordinates. Any point where x<0 or y<0 or weight<0 is ignored.
  This is the only use of the weight in this function.

- object_spacing: the distance between adjacent points in the calibration
  object. A square object is assumed, so the vertical and horizontal distances
  are assumed to be identical. Usually we need the object dimensions in the
  object_height_n,object_width_n arguments, but here we get those from the shape
  of the observations array

- models_or_intrinsics: either

  - a list of mrcal.cameramodel objects from which we use the intrinsics
  - a list of (lensmodel,intrinsics_data) tuples

  These are indexed by icam from indices_frame_camera

RETURNED VALUE

An array of shape (Nobservations,4,3). Each slice is an Rt transformation TO the
camera coordinate system FROM the calibration object coordinate system.

    """

    # I'm given models. I remove the distortion so that I can pass the data
    # on to solvePnP()
    lensmodels_intrinsics_data = [ m.intrinsics() if type(m) is mrcal.cameramodel else m for m in models_or_intrinsics ]
    lensmodels      = [di[0] for di in lensmodels_intrinsics_data]
    intrinsics_data = [di[1] for di in lensmodels_intrinsics_data]

    if not all([mrcal.lensmodel_metadata(m)['has_core'] for m in lensmodels]):
        raise Exception("this currently works only with models that have an fxfycxcy core. It might not be required. Take a look at the following code if you want to add support")

    fx = [ i[0] for i in intrinsics_data ]
    fy = [ i[1] for i in intrinsics_data ]
    cx = [ i[2] for i in intrinsics_data ]
    cy = [ i[3] for i in intrinsics_data ]

    Nobservations = indices_frame_camera.shape[0]

    # Reproject all the observations to a pinhole model
    observations = observations.copy()
    for i_observation in range(Nobservations):
        icam = indices_frame_camera[i_observation,1]

        v = mrcal.unproject(observations[i_observation,...,:2],
                            lensmodels[icam], intrinsics_data[icam])
        observations[i_observation,...,:2] = \
            mrcal.project(v, 'LENSMODEL_PINHOLE',
                          intrinsics_data[icam][:4])

    # this wastes memory, but makes it easier to keep track of which data goes
    # with what
    Rt_cf_all = np.zeros( (Nobservations, 4, 3), dtype=float)

    object_height_n,object_width_n = observations.shape[-3:-1]

    # No calobject_warp. Good-enough for the seeding
    full_object = mrcal.ref_calibration_object(object_width_n, object_height_n, object_spacing)

    for i_observation in range(Nobservations):

        icam = indices_frame_camera[i_observation,1]
        camera_matrix = np.array((( fx[icam], 0,        cx[icam]), \
                                  ( 0,        fy[icam], cy[icam]), \
                                  ( 0,        0,          1.)))

        # shape (Nh,Nw,3)
        d = observations[i_observation, ...]

        # shape (Nh,Nw,6); each row is an x,y,weight pixel observation followed
        # by the xyz coord of the point in the calibration object
        d = nps.glue(d, full_object, axis=-1)

        # shape (Nh*Nw,6)
        d = nps.clump( d, n=2)

        # I pick off those rows where the point observation is valid. Result
        # should be (N,6) where N <= object_height_n*object_width_n
        i = (d[..., 0] >= 0) * (d[..., 1] >= 0) * (d[..., 2] >= 0)
        d = d[i,:]

        # copying because cv2.solvePnP() requires contiguous memory apparently
        observations_local = np.array(d[:,:2][..., np.newaxis])
        ref_object         = np.array(d[:,3:][..., np.newaxis])
        result,rvec,tvec   = cv2.solvePnP(np.array(ref_object),
                                        np.array(observations_local),
                                        camera_matrix, None)
        if not result:
            raise Exception("solvePnP failed!")
        if tvec[2] <= 0:

            # The object ended up behind the camera. I flip it, and try to solve
            # again
            result,rvec,tvec = cv2.solvePnP(np.array(ref_object),
                                            np.array(observations_local),
                                            camera_matrix, None,
                                            rvec, -tvec,
                                            useExtrinsicGuess = True)
            if not result:
                raise Exception("retried solvePnP failed!")
            if tvec[2] <= 0:
                raise Exception("retried solvePnP says that tvec.z <= 0")


        Rt_cf = mrcal.Rt_from_rt(nps.glue(rvec.ravel(), tvec.ravel(), axis=-1))

        # visualize the fit
        # x_cam    = nps.matmult(Rt_cf[:3,:],ref_object)[..., 0] + Rt_cf[3,:]
        # x_imager = x_cam[...,:2]/x_cam[...,(2,)] * focal + (imagersize-1)/2
        # import gnuplotlib as gp
        # gp.plot( (x_imager[:,0],x_imager[:,1], dict(legend='solved')),
        #          (observations_local[:,0,0],observations_local[:,1,0], dict(legend='observed')),
        #          square=1,xrange=(0,4000),yrange=(4000,0),
        #          wait=1)
        # import IPython
        # IPython.embed()
        # sys.exit()

        Rt_cf_all[i_observation, :, :] = Rt_cf

    return Rt_cf_all


def _estimate_camera_poses( calobject_poses_local_Rt_cf, indices_frame_camera, \
                            observations,
                            object_spacing):
    r'''Estimate camera poses in respect to each other

    We are given poses of the calibration object in respect to each observing
    camera. We also have multiple cameras observing the same calibration object
    at the same time, and we have local poses for each. We can thus compute the
    relative camera pose from these observations.

    We have many frames that have different observations from the same set of
    fixed-relative-pose cameras, so we compute the relative camera pose to
    optimize the observations

    Note that this assumes we're solving a calibration problem (stationary
    cameras) observing a moving object, so uses indices_frame_camera, not
    indices_frame_camintrinsics_camextrinsics, which mrcal.optimize() expects
    '''

    import heapq


    object_height_n,object_width_n = observations.shape[-3:-1]
    Ncameras = np.max(indices_frame_camera[:,1]) + 1

    # I need to compute an estimate of the pose of each camera in the coordinate
    # system of camera0. This is only possible if there're enough overlapping
    # observations. For instance if camera1 has overlapping observations with
    # camera2, but neight overlap with camera0, then I can't relate camera1,2 to
    # camera0. However if camera2 has overlap with camera2, then I can compute
    # the relative pose of camera2 from its overlapping observations with
    # camera0. And I can compute the camera1-camera2 pose from its overlapping
    # data, and then transform to the camera0 coord system using the
    # previously-computed camera2-camera0 pose
    #
    # I do this by solving a shortest-path problem using Dijkstra's algorithm to
    # find a set of pair overlaps between cameras that leads to camera0. I favor
    # edges with large numbers of shared observed frames

    # list of camera-i to camera-0 transforms. I keep doing stuff until this
    # list is full of valid data
    Rt_0c = [None] * (Ncameras-1)

    def compute_pairwise_Rt(icam_to, icam_from):

        # I want to assume that icam_from > icam_to. If it's not true, compute the
        # opposite transform, and invert
        if icam_to > icam_from:
            Rt = compute_pairwise_Rt(icam_from, icam_to)
            return mrcal.invert_Rt(Rt)

        if icam_to == icam_from:
            raise Exception("Got icam_to == icam_from ( = {} ). This was probably a mistake".format(icam_to))

        # Now I KNOW that icam_from > icam_to


        Nobservations = indices_frame_camera.shape[0]

        # This is a hack. I look at the correspondence of camera0 to camera i for i
        # in 1:N-1. I ignore all correspondences between cameras i,j if i!=0 and
        # j!=0. Good enough for now
        #
        # No calobject_warp. Good-enough for the seeding
        full_object = mrcal.ref_calibration_object(object_width_n,object_height_n,
                                                       object_spacing)

        A = np.array(())
        B = np.array(())

        # I traverse my observation list, and pick out observations from frames
        # that had data from both my cameras
        iframe_last = -1
        d0  = None
        d1  = None
        Rt0 = None
        Rt1 = None
        for i_observation in range(Nobservations):
            iframe_this,icam_this = indices_frame_camera[i_observation, ...]
            if iframe_this != iframe_last:
                d0  = None
                d1  = None
                Rt0 = None
                Rt1 = None
                iframe_last = iframe_this

            # The cameras appear in order. And above I made sure that icam_from >
            # icam_to, so I take advantage of that here
            if icam_this == icam_to:
                if Rt0 is not None:
                    raise Exception("Saw multiple camera{} observations in frame {}".format(icam_this,
                                                                                            iframe_this))
                Rt0 = calobject_poses_local_Rt_cf[i_observation, ...]
                d0  = observations[i_observation, ..., :2]
            elif icam_this == icam_from:
                if Rt0 is None: # have camera1 observation, but not camera0
                    continue

                if Rt1 is not None:
                    raise Exception("Saw multiple camera{} observations in frame {}".format(icam_this,
                                                                                            iframe_this))
                Rt1 = calobject_poses_local_Rt_cf[i_observation, ...]
                d1  = observations[i_observation, ..., :2]



                # d looks at one frame and has shape (object_height_n,object_width_n,7). Each row is
                #   xy pixel observation in left camera
                #   xy pixel observation in right camera
                #   xyz coord of dot in the calibration object coord system
                d = nps.glue( d0, d1, full_object, axis=-1 )

                # squash dims so that d is (object_height_n*object_width_n,7)
                d = nps.clump(d, n=2)

                ref_object = nps.clump(full_object, n=2)

                # # It's possible that I could have incomplete views of the
                # # calibration object, so I pull out only those point
                # # observations that have a complete view. In reality, I
                # # currently don't accept any incomplete views, and much outside
                # # code would need an update to support that. This doesn't hurt, however

                # # d looks at one frame and has shape (10,10,7). Each row is
                # #   xy pixel observation in left camera
                # #   xy pixel observation in right camera
                # #   xyz coord of dot in the calibration object coord system
                # d = nps.glue( d0, d1, full_object, axis=-1 )

                # # squash dims so that d is (object_height_n*object_width_n,7)
                # d = nps.transpose(nps.clump(nps.mv(d, -1, -3), n=2))

                # # I pick out those points that have observations in both frames
                # i = (d[..., 0] >= 0) * (d[..., 1] >= 0) * (d[..., 2] >= 0) * (d[..., 3] >= 0)
                # d = d[i,:]

                # # ref_object is (N,3)
                # ref_object = d[:,4:]

                A = nps.glue(A, nps.matmult( ref_object, nps.transpose(Rt0[:3,:])) + Rt0[3,:],
                             axis = -2)
                B = nps.glue(B, nps.matmult( ref_object, nps.transpose(Rt1[:3,:])) + Rt1[3,:],
                             axis = -2)

        return mrcal.align_procrustes_points_Rt01(A, B)


    def compute_connectivity_matrix():
        r'''Returns a connectivity matrix of camera observations

        Returns a symmetric (Ncamera,Ncamera) matrix of integers, where each
        entry contains the number of frames containing overlapping observations
        for that pair of cameras

        '''

        camera_connectivity = np.zeros( (Ncameras,Ncameras), dtype=int )
        def finish_frame(i0, i1):
            for ic0 in range(i0, i1):
                for ic1 in range(ic0+1, i1+1):
                    camera_connectivity[indices_frame_camera[ic0,1], indices_frame_camera[ic1,1]] += 1
                    camera_connectivity[indices_frame_camera[ic1,1], indices_frame_camera[ic0,1]] += 1

        f_current       = -1
        i_start_current = -1

        for i in range(len(indices_frame_camera)):
            f,c = indices_frame_camera[i]
            if f < f_current:
                raise Exception("I'm assuming the frame indices are increasing monotonically")
            if f > f_current:
                # first camera in this observation
                f_current = f
                if i_start_current >= 0:
                    finish_frame(i_start_current, i-1)
                i_start_current = i
        finish_frame(i_start_current, len(indices_frame_camera)-1)
        return camera_connectivity


    shared_frames = compute_connectivity_matrix()

    class Node:
        def __init__(self, camera_idx):
            self.camera_idx    = camera_idx
            self.from_idx      = -1
            self.cost_to_node  = None

        def __lt__(self, other):
            return self.cost_to_node < other.cost_to_node

        def visit(self):
            '''Dijkstra's algorithm'''
            self.finish()

            for neighbor_idx in range(Ncameras):
                if neighbor_idx == self.camera_idx                  or \
                   shared_frames[neighbor_idx,self.camera_idx] == 0:
                    continue
                neighbor = nodes[neighbor_idx]

                if neighbor.visited():
                    continue

                cost_edge = Node.compute_edge_cost(shared_frames[neighbor_idx,self.camera_idx])

                cost_to_neighbor_via_node = self.cost_to_node + cost_edge
                if not neighbor.seen():
                    neighbor.cost_to_node = cost_to_neighbor_via_node
                    neighbor.from_idx     = self.camera_idx
                    heapq.heappush(heap, neighbor)
                else:
                    if cost_to_neighbor_via_node < neighbor.cost_to_node:
                        neighbor.cost_to_node = cost_to_neighbor_via_node
                        neighbor.from_idx     = self.camera_idx
                        heapq.heapify(heap) # is this the most efficient "update" call?

        def finish(self):
            '''A shortest path was found'''
            if self.camera_idx == 0:
                # This is the reference camera. Nothing to do
                return

            Rt_fc = compute_pairwise_Rt(self.from_idx, self.camera_idx)

            if self.from_idx == 0:
                Rt_0c[self.camera_idx-1] = Rt_fc
                return

            Rt_0f = Rt_0c[self.from_idx-1]
            Rt_0c[self.camera_idx-1] = mrcal.compose_Rt( Rt_0f, Rt_fc)

        def visited(self):
            '''Returns True if this node went through the heap and has then been visited'''
            return self.camera_idx == 0 or Rt_0c[self.camera_idx-1] is not None

        def seen(self):
            '''Returns True if this node has been in the heap'''
            return self.cost_to_node is not None

        @staticmethod
        def compute_edge_cost(shared_frames):
            # I want to MINIMIZE cost, so I MAXIMIZE the shared frames count and
            # MINIMIZE the hop count. Furthermore, I really want to minimize the
            # number of hops, so that's worth many shared frames.
            cost = 100000 - shared_frames
            assert(cost > 0) # dijkstra's algorithm requires this to be true
            return cost



    nodes = [Node(i) for i in range(Ncameras)]
    nodes[0].cost_to_node = 0
    heap = []

    nodes[0].visit()
    while heap:
        node_top = heapq.heappop(heap)
        node_top.visit()

    if any([x is None for x in Rt_0c]):
        raise Exception("ERROR: Don't have complete camera observations overlap!\n" +
                        ("Past-camera-0 Rt:\n{}\n".format(Rt_0c))                   +
                        ("Shared observations matrix:\n{}\n".format(shared_frames)))


    return np.ascontiguousarray(nps.cat(*Rt_0c))


def estimate_joint_frame_poses(calobject_Rt_camera_frame,
                               extrinsics_Rt_fromref,
                               indices_frame_camera,
                               object_width_n, object_height_n,
                               object_spacing):

    r'''Estimate world-referenced poses of the calibration object

SYNOPSIS

    print( calobject_Rt_camera_frame.shape )
    ===>
    (123, 4,3)

    print( extrinsics_Rt_fromref.shape )
    ===>
    (2, 4,3)
    # We have 3 cameras. The first one is at the reference coordinate system,
    # the pose estimates of the other two are in this array

    print( indices_frame_camera.shape )
    ===>
    (123, 2)

    frames_rt_toref = \
        mrcal.estimate_joint_frame_poses(calobject_Rt_camera_frame,
                                         extrinsics_Rt_fromref,
                                         indices_frame_camera,
                                         object_width_n, object_height_n,
                                         object_spacing)

    print( frames_rt_toref.shape )
    ===>
    (87, 6)

    # We have 123 observations of the calibration object by ANY camera. 87
    # instances of time when the object was observed. Most of the time it was
    # observed by multiple cameras simultaneously, hence 123 > 87

    i_observation = 10
    iframe,icam = indices_frame_camera[i_observation, :]

    # The calibration object in its reference coordinate system
    calobject = mrcal.ref_calibration_object(object_width_n,
                                                 object_height_n,
                                                 object_spacing)

    # The estimated calibration object points in the reference coordinate
    # system, for this one observation
    pref = mrcal.transform_point_rt( frames_rt_toref[iframe],
                                     calobject )

    # The estimated calibration object points in the camera coord system. Camera
    # 0 is at the reference
    if icam >= 1:
        pcam = mrcal.transform_point_Rt( extrinsics_Rt_fromref[icam-1],
                                         pref )
    else:
        pcam = pref

    # The pixel observations we would see if the pose estimates were correct
    q = mrcal.project(pcam, *models[icam].intrinsics())

    # The reprojection error, comparing these hypothesis pixel observations from
    # what we actually observed. This should be small
    err = q - observations[i_observation][:2]

    print( np.linalg.norm(err) )
    ===>
    [something small]

mrcal solves camera calibration problems by iteratively optimizing a nonlinear
least squares problem to bring the pixel observation predictions in line with
actual pixel observations. This requires an initial "seed", an initial estimate
of the solution. This function is a part of that computation. Since this is just
an initial estimate that will be refined, the results of this function do not
need to be exact.

This function ingests an estimate of the camera poses in respect to each other,
and the estimate of the calibration objects in respect to the observing camera.
Most of the time we have simultaneous calibration object observations from
multiple cameras, so this function consolidates all this information to produce
poses of the calibration object in the reference coordinate system, NOT the
observing-camera coordinate system poses we already have.

By convention, we have a "reference" coordinate system that ties the poses of
all the frames (calibration objects) and the cameras together. And by
convention, this "reference" coordinate system is the coordinate system of
camera 0. Thus the array of camera poses extrinsics_Rt_fromref holds Ncameras-1
transformations: the first camera has an identity transformation, by definition.

This function assumes we're observing a moving object from stationary cameras
(i.e. a vanilla camera calibration problem). The mrcal solver is more general,
and supports moving cameras, hence it uses a more general
indices_frame_camintrinsics_camextrinsics array instead of the
indices_frame_camera array used here.

ARGUMENTS

- calobject_Rt_camera_frame: an array of shape (Nobservations,4,3). Each slice
  is an Rt transformation TO the observing camera coordinate system FROM the
  calibration object coordinate system. This is returned by
  estimate_monocular_calobject_poses_Rt_tocam()

- extrinsics_Rt_fromref: an array of shape (Ncameras-1,4,3). Each slice is an Rt
  transformation TO the camera coordinate system FROM the reference coordinate
  system. By convention camera 0 defines the reference coordinate system, so
  that camera's extrinsics are the identity, by definition, and we don't store
  that data in this array

- indices_frame_camera: an array of shape (Nobservations,2) and dtype
  numpy.int32. Each row (iframe,icam) represents an observation at time
  instant iframe of a calibration object by camera icam

- object_width_n: number of horizontal points in the calibration object grid

- object_height_n: number of vertical points in the calibration object grid

- object_spacing: the distance between adjacent points in the calibration
  object. A square object is assumed, so the vertical and horizontal distances
  are assumed to be identical

RETURNED VALUE

An array of shape (Nframes,6). Each slice represents the pose of the calibration
object at one instant in time: an rt transformation TO the reference coordinate
system FROM the calibration object coordinate system.

    '''

    Rt_ref_cam = mrcal.invert_Rt( extrinsics_Rt_fromref )


    def Rt_ref_frame(i_observation0, i_observation1):
        R'''Given a range of observations corresponding to the same frame, estimate the
        pose of that frame

        '''

        def Rt_ref_frame__single_observation(i_observation):
            r'''Transform from the board coords to the reference coords'''
            iframe,icam = indices_frame_camera[i_observation, ...]

            Rt_cam_frame = calobject_Rt_camera_frame[i_observation, :,:]
            if icam == 0:
                return Rt_cam_frame

            return mrcal.compose_Rt( Rt_ref_cam[icam-1, ...], Rt_cam_frame)


        # frame poses should map FROM the frame coord system TO the ref coord
        # system (camera 0).

        # special case: if there's a single observation, I just use it
        if i_observation1 - i_observation0 == 1:
            return Rt_ref_frame__single_observation(i_observation0)

        # Multiple cameras have observed the object for this frame. I have an
        # estimate of these for each camera. I merge them in a lame way: I
        # average out the positions of each point, and fit the calibration
        # object into the mean point cloud
        #
        # No calobject_warp. Good-enough for the seeding
        obj = mrcal.ref_calibration_object(object_width_n, object_height_n,
                                               object_spacing)

        sum_obj_unproj = obj*0
        for i_observation in range(i_observation0, i_observation1):
            Rt = Rt_ref_frame__single_observation(i_observation)
            sum_obj_unproj += mrcal.transform_point_Rt(Rt, obj)

        mean_obj_ref = sum_obj_unproj / (i_observation1 - i_observation0)

        # Got my point cloud. fit

        # transform both to shape = (N*N, 3)
        obj          = nps.clump(obj,  n=2)
        mean_obj_ref = nps.clump(mean_obj_ref, n=2)
        return mrcal.align_procrustes_points_Rt01( mean_obj_ref, obj )




    frames_rt_toref = np.array(())

    iframe_current          = -1
    i_observation_framestart = -1;

    for i_observation in range(indices_frame_camera.shape[0]):
        iframe,icam = indices_frame_camera[i_observation, ...]

        if iframe != iframe_current:
            if i_observation_framestart >= 0:
                Rt = Rt_ref_frame(i_observation_framestart,
                                  i_observation)
                frames_rt_toref = nps.glue(frames_rt_toref,
                                           mrcal.rt_from_Rt(Rt),
                                           axis=-2)

            i_observation_framestart = i_observation
            iframe_current          = iframe

    if i_observation_framestart >= 0:
        Rt = Rt_ref_frame(i_observation_framestart,
                          indices_frame_camera.shape[0])
        frames_rt_toref = nps.glue(frames_rt_toref,
                                   mrcal.rt_from_Rt(Rt),
                                   axis=-2)

    return frames_rt_toref


def make_seed_pinhole( imagersizes,
                       focal_estimate,
                       indices_frame_camera,
                       observations,
                       object_spacing):
    r'''Compute an optimization seed for a camera calibration

SYNOPSIS

    print( imagersizes.shape )
    ===>
    (4, 2)

    print( indices_frame_camera.shape )
    ===>
    (123, 2)

    print( observations.shape )
    ===>
    (123, 3)

    intrinsics_data,       \
    extrinsics_rt_fromref, \
    frames_rt_toref =      \
        mrcal.make_seed_pinhole(imagersizes          = imagersizes,
                                focal_estimate       = 1500,
                                indices_frame_camera = indices_frame_camera,
                                observations         = observations,
                                object_spacing       = object_spacing)

    ....

    mrcal.optimize(intrinsics_data, extrinsics_rt_fromref, frames_rt_toref,
                   lensmodel = 'LENSMODEL_PINHOLE',
                   ...)

mrcal solves camera calibration problems by iteratively optimizing a nonlinear
least squares problem to bring the pixel observation predictions in line with
actual pixel observations. This requires an initial "seed", an initial estimate
of the solution. This function computes a usable seed, and its results can be
fed to mrcal.optimize(). The output of this function is just an initial estimate
that will be refined, so the results of this function do not need to be exact.

This function assumes we have pinhole lenses, and the returned intrinsics apply
to LENSMODEL_PINHOLE. This is usually good-enough to serve as a seed. The
returned intrinsics can be expanded to whatever lens model we actually want to
use prior to invoking the optimizer.

By convention, we have a "reference" coordinate system that ties the poses of
all the frames (calibration objects) and the cameras together. And by
convention, this "reference" coordinate system is the coordinate system of
camera 0. Thus the array of camera poses extrinsics_rt_fromref holds Ncameras-1
transformations: the first camera has an identity transformation, by definition.

This function assumes we're observing a moving object from stationary cameras
(i.e. a vanilla camera calibration problem). The mrcal solver is more general,
and supports moving cameras, hence it uses a more general
indices_frame_camintrinsics_camextrinsics array instead of the
indices_frame_camera array used here.

See test/test-calibration-basic.py and mrcal-calibrate-cameras for usage
examples.

ARGUMENTS

- imagersizes: an iterable of (imager_width,imager_height) iterables. Defines
  the imager dimensions for each camera we're calibrating. May be an array of
  shape (Ncameras,2) or a tuple of tuples or a mix of the two

- focal_estimate: an initial estimate of the focal length of the cameras, in
  pixels. For the purposes of the initial estimate we use the same focal length
  value for both the x and y focal length of ALL the cameras

- indices_frame_camera: an array of shape (Nobservations,2) and dtype
  numpy.int32. Each row (iframe,icam) represents an observation of a
  calibration object by camera icam. iframe is not used by this function

- observations: an array of shape
  (Nobservations,object_height_n,object_width_n,3). Each observation corresponds
  to a row in indices_frame_camera, and contains a row of shape (3,) for each
  point in the calibration object. Each row is (x,y,weight) where x,y are the
  observed pixel coordinates. Any point where x<0 or y<0 or weight<0 is ignored.
  This is the only use of the weight in this function.

- object_spacing: the distance between adjacent points in the calibration
  object. A square object is assumed, so the vertical and horizontal distances
  are assumed to be identical. Usually we need the object dimensions in the
  object_height_n,object_width_n arguments, but here we get those from the shape
  of the observations array

RETURNED VALUES

We return a tuple:

- intrinsics_data: an array of shape (Ncameras,4). Each slice contains the
  pinhole intrinsics for the given camera. These intrinsics are
  (focal_x,focal_y,centerpixel_x,centerpixel_y), and define LENSMODEL_PINHOLE
  model. mrcal refers to these 4 values as the "intrinsics core". For models
  that have such a core (currently, ALL supported models), the core is the first
  4 parameters of the intrinsics vector. So to calibrate some cameras, call
  make_seed_pinhole(), append to intrinsics_data the proper number of parameters
  to match whatever lens model we're using, and then invoke the optimizer.

- extrinsics_rt_fromref: an array of shape (Ncameras-1,6). Each slice is an rt
  transformation TO the camera coordinate system FROM the reference coordinate
  system. By convention camera 0 defines the reference coordinate system, so
  that camera's extrinsics are the identity, by definition, and we don't store
  that data in this array

- frames_rt_toref: an array of shape (Nframes,6). Each slice represents the pose
  of the calibration object at one instant in time: an rt transformation TO the
  reference coordinate system FROM the calibration object coordinate system.

    '''

    def make_intrinsics_vector(imagersize):
        imager_w,imager_h = imagersize
        return np.array( (focal_estimate, focal_estimate,
                          float(imager_w-1)/2.,
                          float(imager_h-1)/2.))

    intrinsics_data = nps.cat( *[make_intrinsics_vector(imagersize) \
                                 for imagersize in imagersizes] )

    # I compute an estimate of the poses of the calibration object in the local
    # coord system of each camera for each frame. This is done for each frame
    # and for each camera separately. This isn't meant to be precise, and is
    # only used for seeding.
    #
    # I get rotation, translation in a (4,3) array, such that R*calobject + t
    # produces the calibration object points in the coord system of the camera.
    # The result has dimensions (N,4,3)
    intrinsics = [('LENSMODEL_PINHOLE', np.array((focal_estimate,focal_estimate, (imagersize[0]-1.)/2,(imagersize[1]-1.)/2,))) \
                  for imagersize in imagersizes]
    calobject_poses_local_Rt_cf = \
        mrcal.estimate_monocular_calobject_poses_Rt_tocam( indices_frame_camera,
                                                           observations,
                                                           object_spacing,
                                                           intrinsics)
    # these map FROM the coord system of the calibration object TO the coord
    # system of this camera

    # I now have a rough estimate of calobject poses in the coord system of each
    # camera. One can think of these as two sets of point clouds, each attached
    # to their camera. I can move around the two sets of point clouds to try to
    # match them up, and this will give me an estimate of the relative pose of
    # the two cameras in respect to each other. I need to set up the
    # correspondences, and mrcal.align_procrustes_points_Rt01() does the rest
    #
    # I get transformations that map points in camera-cami coord system to 0th
    # camera coord system. Rt have dimensions (N-1,4,3)
    camera_poses_Rt_0_cami = \
        _estimate_camera_poses( calobject_poses_local_Rt_cf,
                                indices_frame_camera,
                                observations,
                                object_spacing)

    if len(camera_poses_Rt_0_cami):
        # extrinsics should map FROM the ref coord system TO the coord system of the
        # camera in question. This is backwards from what I have
        extrinsics_Rt_fromref = \
            nps.atleast_dims( mrcal.invert_Rt(camera_poses_Rt_0_cami),
                              -3 )
    else:
        extrinsics_Rt_fromref = np.zeros((0,4,3))

    object_height_n,object_width_n = observations.shape[-3:-1]

    frames_rt_toref = \
        mrcal.estimate_joint_frame_poses(
            calobject_poses_local_Rt_cf,
            extrinsics_Rt_fromref,
            indices_frame_camera,
            object_width_n, object_height_n,
            object_spacing)

    return \
        intrinsics_data, \
        nps.atleast_dims(mrcal.rt_from_Rt(extrinsics_Rt_fromref), -2), \
        frames_rt_toref
