########
#
# cacophony-thermal-importer.py
#
# Create data and metadata for LILA from the Cacophony thermal dataset.  Takes a folder
# of HDF files, and produces .json metadata, along with compressed/normalized videos for
# each HDF file.
#
# Source format notes for this dataset:
#    
# https://docs.google.com/document/d/12sw5JtwdMf9MiXuNCBcvhvZ04Jwa1TH2Lf6LnJmF8Bk/edit
#
########

#%% Imports and constants

import os
import h5py
import numpy as np
import json

from tqdm import tqdm
from copy import deepcopy
from collections import defaultdict

import cv2

from multiprocessing.pool import Pool

base_dir = '/bigdata/home/sftp/cacophony-ferraro_/data/cacophony-thermal/'
output_base = os.path.expanduser('~/tmp/tmp/cacophony-thermal-out')
os.makedirs(output_base,exist_ok=True)

# Every HDF file specifies a crop rectangle within which the pixels are trustworthy;
# in practice this is the same across all files.
expected_crop_rectangle = [1,1,159,119]

# Required attributes for each video
expected_clip_attributes = ['clip_id', 'crop_rectangle', 'ffc_frames', 'frame_temp_max', 
                            'frame_temp_mean', 'frame_temp_median', 'frame_temp_min', 'max_temp', 
                            'mean_temp', 'min_temp', 'num_frames', 'res_x', 'res_y', 'start_time',
                            'station_id']

# Attributes that may or may not be present for each video
optional_clip_attributes = ['temp_thresh','model']

# Required attributes for each trck
expected_track_attributes = ['end_frame', 'id', 'start_frame']

# Attributes that may or may not be present for each track
optional_track_attributes = ['human_tag', 'human_tag_confidence', 'human_tags', 
                             'human_tags_confidence', 'ai_tag', 'ai_tag_confidence']

frame_rate = 9

use_default_filtering = False
write_as_color = False

# codec = 'ffv1'
# codec = 'hfyu'
codec = 'mp4v'

codec_to_extension = {'mp4v':'.mp4','ffv1':'.avi','hfyu':'.avi'}

# Set to >0 to process only a subset of clips
debug_n = -1
n_workers = 16


#%% Support functions

def remove_tracking_points(clip_metadata):
    """
    As a debugging convenience, take the metadata for a clip (after conversion to
    the output format) and remove the only field that makes it hard to read in a
    console (the track coordinates).
    """
    
    slim_metadata = deepcopy(clip_metadata)
    for t in slim_metadata['tracks']:
        del t['points']
    return slim_metadata

    
def norm_image(image,vmin=None,vmax=None,do_normalization=True,stack_channels=True):
    """
    Normalize an MxN 2D numpy ndarray (may be any type, but typically uint16) into the range 
    0,255.  
    
    If stack_channels==True, return as an MxNx3 uint8 matrix (content is replicated across 
    all three channels).
    """
    
    if vmin is not None:
        assert vmax is not None
        assert vmax > vmin
    if vmax is not None:
        assert vmin is not None

    assert isinstance(image,np.ndarray)
    assert isinstance(image[0][0],np.uint16) or isinstance(image[0][0],np.float32), \
        'First pixel is of type {}'.format(type(image[0][0]))
    assert len(image.shape) == 2
    
    norm = np.float32(image)
    
    if do_normalization:
        
        if vmin is None:
            vmin = np.amin(image)
            vmax = np.amax(image)
            
        norm = 255 * (norm - vmin) / (vmax - vmin)

    norm = np.uint8(norm)
    norm = norm[:, :, np.newaxis]
    if stack_channels:
        norm = np.repeat(norm, 3, axis=2)
    return norm


#%% Enumerate files

from md_utils import path_utils
all_files = path_utils.recursive_file_list(base_dir)
all_hdf_files_relative = [os.path.relpath(fn,base_dir) for fn in all_files if fn.lower().endswith('.hdf5')]

print('Found {} HDF files (of {} total files)'.format(
    len(all_hdf_files_relative),len(all_files)))


#%% Process one file

def process_file(fn_relative,verbose=False):
    """
    Read the HDF file, convert to video files with/without filtering, and return 
    a metadata dict for this file.
    """
    
    fn_abs = os.path.join(base_dir,fn_relative)
    
    clip_id = int(os.path.basename(fn_relative).split('.')[0])
    metadata_fn = os.path.join(output_base,str(clip_id) + '_metadata.json')
    
    clip_metadata = {}
    clip_metadata['hdf_filename'] = os.path.basename(fn_relative)    
    clip_metadata['id'] = clip_id
    clip_metadata['error'] = None
        
    try:
        h5f = h5py.File(fn_abs, "r")
    except Exception as e:
        print('Could not open file {}: {}'.format(
            fn_relative,str(e)))
        clip_metadata['error'] = str(e)
        with open(metadata_fn,'w') as f:
            json.dump(clip_metadata,f,indent=1)        
        return clip_metadata    
    
    clip_attrs = h5f.attrs
    
    for s in expected_clip_attributes:
        assert s in clip_attrs
                
    assert clip_id == int(clip_attrs.get('clip_id'))
    assert os.path.basename(fn_relative).startswith(str(clip_id))
    
    station_id = clip_attrs.get('station_id')
    assert isinstance(station_id,np.int64)
    station_id = int(station_id)
    
    crop_rectangle = clip_attrs.get('crop_rectangle')
    assert len(crop_rectangle) == 4
    for i_coord in range(0,4):
        assert crop_rectangle[i_coord] == expected_crop_rectangle[i_coord]
      
    frames = h5f['frames']
    assert 'thermals' in frames
    
    # This is an HDF dataset of size n_frames,y,x
    thermal_frames = frames['thermals']
    assert len(thermal_frames.shape) == 3
    
    # If present, this is an HDF dataset of size y,x
    if 'background' in frames:
        background_frame = frames['background']
        assert len(background_frame.shape) == 2
        assert background_frame.shape[0] == thermal_frames.shape[1]
        assert background_frame.shape[1] == thermal_frames.shape[2]
    else:
        background_frame = None
    calibration_frame_indices = clip_attrs.get('ffc_frames')
    
    if len(calibration_frame_indices) > 0:
        assert max(calibration_frame_indices) < thermal_frames.shape[0]
    
    assert clip_attrs.get('num_frames') == thermal_frames.shape[0]
    assert clip_attrs.get('res_x') == thermal_frames.shape[2]
    assert clip_attrs.get('res_y') == thermal_frames.shape[1]    
    assert clip_attrs.get('model') in [None,'lepton3.5','lepton3']
    
    tracks = h5f['tracks']
    
    track_ids = list(tracks.keys())
    
    # List of dicts
    tracks_this_clip = []
    
    # i_track = 0; track_id = track_ids[i_track]
    for i_track,track_id in enumerate(track_ids):
        
        track = tracks[track_id]
        
        if 'human_tags' not in track.attrs.keys():
            continue
        
        track_info = {}
            
        # 'human_tags' is all the tags that were assigned to this track by humans
        # 'human_tags_confidence' is the confidence for each of those assignments
        #
        # If there is a clear "winner", 'human_tag' and 'human_tag' confidence will
        # identify the clear winner.
        if 'human_tag' in track.attrs.keys():
            
            assert 'human_tags' in track.attrs.keys()
            assert 'human_tags_confidence' in track.attrs.keys()
            assert 'human_tag_confidence' in track.attrs.keys()            
        
        track_tags = []
        
        if 'human_tags' in track.attrs.keys():
            
            assert 'human_tags_confidence' in track.attrs.keys()            
            assert len(track.attrs.get('human_tags_confidence')) == \
                   len(track.attrs.get('human_tags'))            
            
            human_tags_this_clip = list(track.attrs.get('human_tags'))
            human_tag_confidences_this_clip = list(track.attrs.get('human_tags_confidence'))
            
            for i_tag,tag in enumerate(human_tags_this_clip):
                assert isinstance(tag,str)
                tag_info = {}
                tag_info['label'] = tag
                conf = float(human_tag_confidences_this_clip[i_tag])
                tag_info['confidence'] = conf
                track_tags.append(tag_info)
                
        track_start_frame = int(round(track.attrs.get('start_frame')))
        track_end_frame = int(round(track.attrs.get('end_frame')))
        track_info['start_frame'] = track_start_frame
        track_info['end_frame'] = track_end_frame
        track_info['tags'] = track_tags
        
        # A list of x/y/frame tuples
        track_info['points'] = []
        
        for s in expected_track_attributes:
            assert s in track.attrs
        
        positions = track['regions']
        
        # Positions is an N x 7 matrix in which each row looks like:
        #
        # [left,top,right,bottom,frame_number,mass,blank_frame]
        #
        # The origin appears to be in the upper-left.
        # 
        # "blank_frame" indicates that the tracked object is not visible in this frame,
        # but was predicted from previous frames.
        assert positions.shape[1] == 7        
        
        # The number of items in the positions array should be equal to the length of the track, but this
        # can be off by a little when 'start_frame' and/or 'end_frame' are not integers.  Make sure this
        # is approximately true.
        
        # assert positions.shape[0] == 1 + (track.attrs.get('end_frame') - track.attrs.get('start_frame'))
        track_length_error = abs(positions.shape[0] - 
            (1 + (track.attrs.get('end_frame') - track.attrs.get('start_frame'))))
        assert track_length_error < 2
        
        # i_position = 0; position = positions[i_position]
        for i_position,position in enumerate(positions):
            
            left = position[0]
            top = position[1]
            right = position[2]
            bottom = position[3]
            frame_number = position[4]
            
            # TODO: I'm being lazy about the fact that these don't reflect the
            # pixels cropped out of the border.  IMO this is OK because for this dataset,
            # this is just an approximate set of coordinates used to disambiguate simultaneous 
            # areas of movement when multiple different labels are present in the same video.
            position_info = [float((right-left)/2),
                             float((bottom-top)/2),
                             int(frame_number)]
            track_info['points'].append(position_info)
            
            # TODO: remove
            # if left > right:
            #   left_right_issues.append((i_file,i_track,i_position))
            
            # TODO: remove
            # if top > bottom:
            #    top_bottom_issues.append((i_file,i_track,i_position))
            
            boxes_can_extend_beyond_frame = False
            
            if boxes_can_extend_beyond_frame:
                tolerance = 5
                assert left >= -1 * tolerance
                assert right >= -1 * tolerance
                assert top >= -1 * tolerance
                assert bottom >= -1 * tolerance                
            else:
                if left <= right:
                    assert left >= 0 and left < clip_attrs.get('res_x')
                    assert right >= 0 and right < clip_attrs.get('res_x')
                
                if top <= bottom:
                    assert top >= 0 and top < clip_attrs.get('res_y')
                    assert bottom >= 0 and bottom < clip_attrs.get('res_y')
            
            # frame_number should be approximately equal to i_position + start_frame, but this
            # can be off by a little when 'start_frame' and/or 'end_frame' are not integers. 
            # Make sure this is approximately true.
            
            # assert frame_number == i_position + track.attrs.get('start_frame')
            frame_number_error = abs(frame_number - (i_position + track.attrs.get('start_frame')))
            assert frame_number_error <= 2
        
        # ...for each position in this track
        
        tracks_this_clip.append(track_info)
        
    # ...for each track ID                
    
    clip_metadata['tracks'] = tracks_this_clip
    
    assert len(human_tags_this_clip) > 0
    
    ffc_frames = clip_attrs.get('ffc_frames').tolist()
    if len(ffc_frames) > 0:
        assert max(ffc_frames) < thermal_frames.shape[0]
        n_ffc_frames = len(ffc_frames)
        n_frames = thermal_frames.shape[0]
        if verbose:
            if (n_ffc_frames / n_frames) > 0.2:
                print('Warning: in video {}, {} of {} frames are FFC frames (tags: {})'.format(
                    fn_relative,n_ffc_frames,n_frames,str(human_tags_this_clip)))                
    
    frames = h5f["frames"]
    
    if "background" in frames:
        background = frames["background"]
        background_frame_present = True        
    else:
        background = frames["thermals"][0]
        background_frame_present = False
    
    crop_rectangle = clip_attrs["crop_rectangle"]
    background = background[
        crop_rectangle[1]:crop_rectangle[3],
        crop_rectangle[0]:crop_rectangle[2]
    ]
    
    # Compute the median frame value
    #
    # (...which we may use for filtering)
    
    frames_array = np.array(frames['thermals'])
    frames_array = frames_array[:,crop_rectangle[1] : crop_rectangle[3], crop_rectangle[0] : crop_rectangle[2]]
    median_values = np.float32(np.median(frames_array,0))
    
    if (background_frame_present or use_default_filtering):
        background_for_filtering = background
    else:
        if verbose:
            print('No background present: using median values for background')
        background_for_filtering = median_values
    
    # Find the largest value by which any pixel in this video exceeds the background 
    #
    # (...which we may use for normalization)
    
    max_pixel_diff = 0
    
    for frame in frames["thermals"]:
        cropped_frame = frame[
            crop_rectangle[1]:crop_rectangle[3],
            crop_rectangle[0]:crop_rectangle[2]
        ]
        
        filtered_frame = np.float32(cropped_frame) - background_for_filtering
        max_pixel_diff_this_frame = np.amax(filtered_frame)
        if max_pixel_diff_this_frame > max_pixel_diff:
            max_pixel_diff = max_pixel_diff_this_frame
    
    filtered_frames = []
    original_frames = []
    
    # i_frame = 0; frame = frames["thermals"][i_frame]
    for i_frame,frame in enumerate(frames["thermals"]):
        
        cropped_frame = frame[crop_rectangle[1] : crop_rectangle[3], crop_rectangle[0] : crop_rectangle[2]]

        # Subtract the background frame
        filtered_frame = np.float32(cropped_frame) - background_for_filtering
        
        # Assume that nothing can be cooler than the background
        filtered_frame[filtered_frame < 0] = 0
        
        # Normalize filtered frame (and convert to three channels)
        
        if use_default_filtering:
            filtered_frame = norm_image(filtered_frame,stack_channels=write_as_color)
        else:        
            filtered_frame = norm_image(filtered_frame,vmin=0,vmax=max_pixel_diff,stack_channels=write_as_color)
        
        # Normalize original frame (and convert to three channels)
        
        original_frame = norm_image(cropped_frame,stack_channels=write_as_color)
                
        filtered_frames.append(filtered_frame)        
        original_frames.append(original_frame)
        
    # ...for each frame

    # filtered_frames[0].shape[1] is 158, clip_attrs.get('res_x') is 160, ergo shape is h,w
    video_w = filtered_frames[0].shape[1]
    video_h = filtered_frames[0].shape[0]
    
    filtered_video_fn = os.path.join(output_base,str(clip_id) + '_filtered' + codec_to_extension[codec])    
    filtered_video_out = cv2.VideoWriter(filtered_video_fn, cv2.VideoWriter_fourcc(*codec), frame_rate, 
                          (video_w, video_h), isColor=write_as_color)

    for i_frame,filtered_frame in enumerate(filtered_frames): 
        filtered_video_out.write(filtered_frame)
    filtered_video_out.release()
    
    unfiltered_video_fn = os.path.join(output_base,str(clip_id) + codec_to_extension[codec])    
    unfiltered_video_out = cv2.VideoWriter(unfiltered_video_fn, cv2.VideoWriter_fourcc(*codec), frame_rate, 
                          (video_w, video_h), isColor=write_as_color)

    for i_frame,frame in enumerate(original_frames): 
        unfiltered_video_out.write(frame)
    unfiltered_video_out.release()
        
    metadata_fn = os.path.join(output_base,str(clip_id) + '_metadata.json')
    
    # clip_metadata['id'] = clip_id
    # clip_metadata['hdf_filename'] = os.path.basename(fn_relative)
    
    clip_metadata['video_filename'] = os.path.basename(unfiltered_video_fn)
    clip_metadata['filtered_video_filename'] = os.path.basename(filtered_video_fn)
    clip_metadata['location'] = station_id
    clip_metadata['calibration_frames'] = ffc_frames
    
    with open(metadata_fn,'w') as f:
        json.dump(clip_metadata,f,indent=1)
            
    return clip_metadata

# ...process_file(...)


#%% Process files

if debug_n > 0:
    files_to_process = all_hdf_files_relative[0:debug_n]
else:
    files_to_process = all_hdf_files_relative
    
if n_workers <= 1:
    
    all_clip_metadata = []    
    for i_file,fn_relative in tqdm(enumerate(files_to_process),total=len(files_to_process)):    
        clip_metadata = process_file(fn_relative)
        all_clip_metadata.append(clip_metadata)
        
else:
    
    pool = Pool(n_workers)
    all_clip_metadata = list(tqdm(pool.imap(process_file,files_to_process),
                                  total=len(files_to_process)))
        
    
#%% Postprocessing

failed_file_to_error = {}

label_to_video_count = defaultdict(int)

# clip_metadata = all_clip_metadata[0]
for clip_metadata in all_clip_metadata:
    
    if clip_metadata['error'] is not None:
        failed_file_to_error[clip_metadata['hdf_filename']] = clip_metadata['error']
        continue

    labels_this_clip = set()
    
    # track_info = clip_metadata['tracks'][0]
    for track_info in clip_metadata['tracks']:
        for tag in track_info['tags']:
            tag_label = tag['label']
            labels_this_clip.add(tag_label)
    
    for label in labels_this_clip:
        label_to_video_count[label] += 1
                    
# ...for each clip

label_to_video_count = {k: v for k, v in sorted(label_to_video_count.items(), 
                                                key=lambda item: item[1], reverse=True)}

print('Failed to open {} of {} files'.format(
    len(failed_file_to_error),len(all_hdf_files_relative)))

print('Labels:\n')

for label in label_to_video_count:
    print('{}: {}'.format(label,label_to_video_count[label]))


#%% Scrap

if False:

    pass

    #%%

    i_file = 8; fn_relative = all_hdf_files_relative[i_file]
    clip_metadata = process_file(fn_relative)
    
    
    #%% Choose a random video with a particular label

    target_label = 'pukeko'    
    target_clips = []
    
    for clip_metadata in all_clip_metadata:    
        
        if clip_metadata['error'] is not None:
            continue
    
        labels_this_clip = set()
        
        # track_info = clip_metadata['tracks'][0]
        for track_info in clip_metadata['tracks']:
            for tag in track_info['tags']:
                tag_label = tag['label']
                labels_this_clip.add(tag_label)
            
        if target_label in labels_this_clip:            
            target_clips.append(clip_metadata)
    
    print('Found {} matches'.format(len(target_clips)))

    import random
    selected_clip = random.choice(target_clips)
    filtered_video_filename = selected_clip['filtered_video_filename']
    video_filename = selected_clip['video_filename']
    
    from md_utils.path_utils import open_file
    # open_file(os.path.join(output_base,video_filename))
    open_file(os.path.join(output_base,filtered_video_filename))
    
    # import clipboard; clipboard.copy(os.path.join(output_base,video_filename))


    #%% Look for clips with multiple different labels
    
    for i_clip,clip_metadata in enumerate(all_clip_metadata): 
        
        if clip_metadata['error'] is not None:
            continue
    
        labels_this_clip = set()
        
        labels_to_ignore = ['false-positive','unidentified','part','poor tracking']
        
        # track_info = clip_metadata['tracks'][0]
        for track_info in clip_metadata['tracks']:
            for tag in track_info['tags']:
                tag_label = tag['label']
                if tag_label not in labels_to_ignore:
                    labels_this_clip.add(tag_label)                
    
        assert len(labels_this_clip) <= 3
                
        if len(labels_this_clip) > 1:
            print('Clip {} has {} labels: {}'.format(
                i_clip,len(labels_this_clip),str(labels_this_clip)))
        
        # remove_tracking_points(clip_metadata)