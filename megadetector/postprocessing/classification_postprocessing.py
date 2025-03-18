"""

classification_postprocessing.py

Functions for postprocessing species classification results, particularly:
    
* Smoothing results within an image (an image with 700 cows and one deer is really just 701
  cows)
* Smoothing results within a sequence (a sequence that looks like deer/deer/deer/elk/deer/deer
  is really just a deer)
  
"""

#%% Constants and imports

import json
import copy

from collections import defaultdict
from tqdm import tqdm

from megadetector.utils.ct_utils import is_list_sorted
from megadetector.utils.wi_utils import clean_taxonomy_string


#%% Options classes

class ClassificationSmoothingOptions:
    """
    Options used to parameterize smooth_classification_results_image_level()
    and smooth_classification_results_sequence_level()
    """

    def __init__(self):
        
        #: How many detections do we need in a dominant category to overwrite 
        #: non-dominant classifications?
        self.min_detections_to_overwrite_secondary = 4
    
        #: Even if we have a dominant class, if a non-dominant class has at least 
        #: this many classifications in an image, leave them alone.
        #:
        #: If this is <= 1, we won't replace non-dominant, non-other classes
        #: with the dominant class, even if there are 900 cows and 1 deer.
        self.max_detections_nondominant_class = 1
    
        #: If the dominant class has at least this many classifications, overwrite 
        #: "other" classifications with the dominant class
        self.min_detections_to_overwrite_other = 2
        
        #: Names to treat as "other" categories; can't be None, but can be empty
        #:
        #: "Other" classifications will be changed to the dominant category, regardless
        #: of confidence, as long as there are at least min_detections_to_overwrite_other 
        #: examples of the dominant class.  For example, cow/other will remain unchanged,
        #: but cow/cow/other will become cow/cow/cow.
        self.other_category_names = ['other','unknown','no cv result','animal','blank','mammal']
    
        #: We're not even going to mess around with classifications below this threshold.
        #:
        #: We won't count them, we won't over-write them, they don't exist during the
        #: within-image smoothing step.
        self.classification_confidence_threshold = 0.5
    
        #: We're not even going to mess around with detections below this threshold.
        #:
        #: We won't count them, we won't over-write them, they don't exist during the
        #: within-image smoothing step.
        self.detection_confidence_threshold = 0.15
        
        #: If classification descriptions are present and appear to represent taxonomic
        #: information, should we propagate classifications when lower-level taxa are more 
        #: common in an image?  For example, if we see "carnivore/fox/fox/deer", should 
        #: we make that "fox/fox/fox/deer"?
        self.propagate_classifications_through_taxonomy = True
        
        #: Should we record information about the state of labels prior to smoothing?
        self.add_pre_smoothing_description = True
        
        #: When a dict (rather than a file) is passed to either smoothing function,
        #: if this is True, we'll make a copy of the input dict before modifying.
        self.modify_in_place = False


#%% Utility functions

def _results_for_sequence(images_this_sequence,filename_to_results):
    """
    Fetch MD results for every image in this sequence, based on the 'file_name' field
    """
    
    results_this_sequence = []
    for im in images_this_sequence:
        fn = im['file_name']
        results_this_image = filename_to_results[fn]
        assert isinstance(results_this_image,dict)
        results_this_sequence.append(results_this_image)
        
    return results_this_sequence
            
    
def _sort_images_by_time(images):
    """
    Returns a copy of [images], sorted by the 'datetime' field (ascending).
    """
    return sorted(images, key = lambda im: im['datetime'])        


def _count_detections_by_category(detections,options):
    """
    Count the number of instances of each category in the detections list
    [detections] that have an above-threshold detection.  Sort results in descending 
    order by count.  Returns a dict mapping category ID --> count.  If no detections
    are above threshold, returns an empty dict.
    
    Assumes that if the 'classifications' field is present for a detection, it has
    length 1, i.e. that non-top classifications have already been removed.
    """
    
    category_to_count = defaultdict(int)
    
    for det in detections:
        if ('classifications' in det) and (det['conf'] >= options.detection_confidence_threshold):
            assert len(det['classifications']) == 1
            c = det['classifications'][0]
            if c[1] >= options.classification_confidence_threshold:
                category_to_count[c[0]] += 1            
                    
    category_to_count = {k: v for k, v in sorted(category_to_count.items(),
                                                 key=lambda item: item[1], 
                                                 reverse=True)}
    
    return category_to_count


def _get_description_string(category_to_count,classification_descriptions):
    """
    Return a string summarizing the image content according to [category_to_count].
    """
    
    category_strings = []
    # category_id = next(iter(category_to_count))
    for category_id in category_to_count:
        category_description = classification_descriptions[category_id]
        tokens = category_description.split(';')
        assert len(tokens) == 7
        category_name = tokens[-1]
        if len(category_name) == 0:
            category_name = 'undefined category'
        count = category_to_count[category_id]
        category_string = '{} ({})'.format(category_name,count)
        category_strings.append(category_string)
    
    return ', '.join(category_strings)
    

def _print_counts_with_names(category_to_count,classification_descriptions):
    """
    Print a list of classification categories with counts, based in the name --> count
    dict [category_to_count]
    """
    
    for category_id in category_to_count:
        category_name = classification_descriptions[category_id]
        count = category_to_count[category_id]
        print('{}: {} ({})'.format(category_id,category_name,count))
    
    
def _prepare_results_for_smoothing(input_file,options):
    """
    Load results from [input_file] if necessary, prepare category descrptions 
    for smoothing.  Adds pre-smoothing descriptions to every image if the options
    say we're supposed to do that.
    """
        
    if isinstance(input_file,str):
        with open(input_file,'r') as f:
            print('Loading results from:\n{}'.format(input_file))
            d = json.load(f)
    else:
        assert isinstance(input_file,dict)
        if options.modify_in_place:
            d = input_file
        else:
            print('modify_in_place is False, copying the input before modifying')
            d = copy.deepcopy(input_file)


    ## Category processing
    
    category_name_to_id = {d['classification_categories'][k]:k for k in d['classification_categories']}
    other_category_ids = []
    for s in options.other_category_names:
        if s in category_name_to_id:
            other_category_ids.append(category_name_to_id[s])
        
    # Before we do anything else, get rid of everything but the top classification
    # for each detection, and remove the 'classifications' field from detections with
    # no classifications.
    for im in tqdm(d['images']):
        
        if 'detections' not in im or im['detections'] is None or len(im['detections']) == 0:
            continue
        
        detections = im['detections']
        
        for det in detections:
            
            if 'classifications' not in det:
                continue
            if len(det['classifications']) == 0:
                del det['classifications']
                continue
            
            classification_confidence_values = [c[1] for c in det['classifications']]
            assert is_list_sorted(classification_confidence_values,reverse=True)
            det['classifications'] = [det['classifications'][0]]
    
        # ...for each detection in this image
        
    # ...for each image
    
    
    ## Clean up classification descriptions so we can test taxonomic relationships
    ## by substring testing.
    
    classification_descriptions_clean = None
    classification_descriptions = None
    
    if 'classification_category_descriptions' in d:
        classification_descriptions = d['classification_category_descriptions']
        classification_descriptions_clean = {}
        # category_id = next(iter(classification_descriptions))
        for category_id in classification_descriptions:            
            classification_descriptions_clean[category_id] = \
                clean_taxonomy_string(classification_descriptions[category_id]).strip(';').lower()
    
    
    ## Optionally add pre-smoothing descriptions to every image
    
    if options.add_pre_smoothing_description:
        
        for im in tqdm(d['images']):
            
            if 'detections' not in im or im['detections'] is None or len(im['detections']) == 0:
                continue
            
            detections = im['detections']        
            category_to_count = _count_detections_by_category(detections, options)
                    
            im['pre_smoothing_description'] = \
                _get_description_string(category_to_count, classification_descriptions)
    
    
    return {
        'd':d,
        'other_category_ids':other_category_ids,
        'classification_descriptions_clean':classification_descriptions_clean,
        'classification_descriptions':classification_descriptions
    }

# ...def _prepare_results_for_smoothing(...)    


def _smooth_classifications_for_list_of_detections(detections,
                                                   options,
                                                   other_category_ids,
                                                   classification_descriptions,
                                                   classification_descriptions_clean):
    """
    Smooth classifications for a list of detections, which may have come from a single
    image, or may represent an entire sequence.
    
    Returns None if no changes are made, else a dict.
    
    classification_descriptions_clean should be semicolon-delimited taxonomic strings 
    from which common names and GUIDs have already been removed.
    
    Assumes there is only one classification per detection, i.e. that non-top classifications
    have already been remoevd.    
    """
        
    ## Count the number of instances of each category in this image
    
    category_to_count = _count_detections_by_category(detections, options)
    # _print_counts_with_names(category_to_count,classification_descriptions)
    # _get_description_string(category_to_count, classification_descriptions)
        
    if len(category_to_count) <= 1:
        return None
    
    keys = list(category_to_count.keys())
    
    # Handle a quirky special case: if the most common category is "other" and 
    # it's "tied" with the second-most-common category, swap them
    if (len(keys) > 1) and \
        (keys[0] in other_category_ids) and \
        (keys[1] not in other_category_ids) and \
        (category_to_count[keys[0]] == category_to_count[keys[1]]):
            keys[1], keys[0] = keys[0], keys[1]
            
    max_count = category_to_count[keys[0]]    
    most_common_category = keys[0]
    del keys
    
    
    ## Possibly change "other" classifications to the most common category
    
    # ...if the dominant category is not an "other" category.
    
    n_other_classifications_changed_this_image = 0
    
    # If we have at least *min_detections_to_overwrite_other* in a category that isn't
    # "other", change all "other" classifications to that category
    if (max_count >= options.min_detections_to_overwrite_other) and \
        (most_common_category not in other_category_ids):
        
        for det in detections:
            
            if ('classifications' in det) and \
                (det['conf'] >= options.detection_confidence_threshold): 
                
                assert len(det['classifications']) == 1
                c = det['classifications'][0]
                    
                if c[1] >= options.classification_confidence_threshold and \
                    c[0] in other_category_ids:
                        
                    n_other_classifications_changed_this_image += 1
                    c[0] = most_common_category
                                        
            # ...if there are classifications for this detection
            
        # ...for each detection
                
    # ...if we should overwrite all "other" classifications
    
    
    ## Re-count
    
    category_to_count = _count_detections_by_category(detections, options)
    # _print_counts_with_names(category_to_count,classification_descriptions)
    keys = list(category_to_count.keys())
    max_count = category_to_count[keys[0]]    
    most_common_category = keys[0]
    del keys
    
    ## At this point, we know we have a dominant category; possibly change 
    ## above-threshold classifications to that category.
    
    n_detections_flipped_this_image = 0
    
    # Don't do this if the most common category is an "other" category
    if most_common_category not in other_category_ids:
        
        # det = detections[0]
        for det in detections:
            
            if ('classifications' in det) and \
                (det['conf'] >= options.detection_confidence_threshold):
                
                assert len(det['classifications']) == 1
                c = det['classifications'][0]
                
                if (c[1] >= options.classification_confidence_threshold) and \
                   (c[0] != most_common_category) and \
                   (max_count > category_to_count[c[0]]) and \
                   (max_count > options.min_detections_to_overwrite_secondary) and \
                   (category_to_count[c[0]] <= options.max_detections_nondominant_class):
                        
                    c[0] = most_common_category
                    n_detections_flipped_this_image += 1
                                        
            # ...if there are classifications for this detection
            
        # ...for each detection

    # ...if the dominant category is legit    
    
    
    ## Re-count
    
    category_to_count = _count_detections_by_category(detections, options)
    # _print_counts_with_names(category_to_count,classification_descriptions)
    keys = list(category_to_count.keys())
    max_count = category_to_count[keys[0]]    
    most_common_category = keys[0]
    del keys
    
    
    ## Possibly collapse higher-level taxonomic predictions down to lower levels
    
    n_taxonomic_changes_this_image = 0
        
    if (classification_descriptions_clean is not None) and \
        (len(classification_descriptions_clean) > 0) and \
        (len(category_to_count) > 1):
    
        # det = detections[3]
        for det in detections:
            
            if ('classifications' in det) and \
                (det['conf'] >= options.detection_confidence_threshold):
                
                assert len(det['classifications']) == 1
                c = det['classifications'][0]
                
                # Don't bother with any classifications below the confidence threshold
                if c[1] < options.classification_confidence_threshold:
                    continue
    
                category_id_this_classification = c[0]
                assert category_id_this_classification in category_to_count
                
                category_description_this_classification = \
                    classification_descriptions_clean[category_id_this_classification]
                
                count_this_category = category_to_count[category_id_this_classification]
                
                # Are there any child categories with more classifications?
                for category_id_of_candidate_child in category_to_count.keys():
                    
                    if category_id_of_candidate_child == category_id_this_classification:
                        continue
                    
                    category_description_candidate_child = \
                        classification_descriptions_clean[category_id_of_candidate_child]
                    
                    # An empty description corresponds to "animal", which can never
                    # be a child of another category.
                    if len(category_description_candidate_child) == 0:
                        continue
                        
                    # Parent/child taxonomic relationships are defined by a substring relationship
                    is_child = category_description_this_classification in \
                        category_description_candidate_child
                    if not is_child:
                        continue
                    
                    child_category_count = category_to_count[category_id_of_candidate_child]
                    if child_category_count > count_this_category:
                        
                        c[0] = category_id_of_candidate_child
                        n_taxonomic_changes_this_image += 1
                    
                # ...for each classification
                
            # ...if there are classifications for this detection
            
        # ...for each detection
        
    # ...if we have taxonomic information available    
    
    return {'n_other_classifications_changed_this_image':n_other_classifications_changed_this_image,
            'n_detections_flipped_this_image':n_detections_flipped_this_image,
            'n_taxonomic_changes_this_image':n_taxonomic_changes_this_image}


def _smooth_single_image(im,
                         options,
                         other_category_ids,
                         classification_descriptions,
                         classification_descriptions_clean):
    """
    Smooth classifications for a single image.  Returns None if no changes are made,
    else a dict.
    
    classification_descriptions_clean should be semicolon-delimited taxonomic strings 
    from which common names and GUIDs have already been removed.
    
    Assumes there is only one classification per detection, i.e. that non-top classifications
    have already been remoevd.
    """
    
    # Useful debug snippet
    #
    # if 'filename' in im['file']:
    #    import pdb; pdb.set_trace()    
    
    
    if 'detections' not in im or im['detections'] is None or len(im['detections']) == 0:
        return
    
    detections = im['detections']
        
    return _smooth_classifications_for_list_of_detections(detections, 
        options=options, 
        other_category_ids=other_category_ids,
        classification_descriptions=classification_descriptions, 
        classification_descriptions_clean=classification_descriptions_clean)

# ...def smooth_single_image


#%% Image-level smoothing

def smooth_classification_results_image_level(input_file,output_file=None,options=None):
    """
    Smooth classifications at the image level for all results in the MD-formatted results
    file [input_file], optionally writing a new set of results to [output_file].
    
    This function generally expresses the notion that an image with 700 cows and one deer 
    is really just 701 cows.
    
    Only count detections with a classification confidence threshold above
    [options.classification_confidence_threshold], which in practice means we're only
    looking at one category per detection.
    
    If an image has at least [options.min_detections_to_overwrite_secondary] such detections
    in the most common category, and no more than [options.max_detections_nondominant_class]
    in the second-most-common category, flip all detections to the most common
    category.
    
    Optionally treat some classes as particularly unreliable, typically used to overwrite an 
    "other" class.
    
    This function also removes everything but the non-dominant classification for each detection.
    
    Args:
        input_file (str): MegaDetector-formatted classification results file to smooth.  Can
            also be an already-loaded results dict.
        output_file (str, optional): .json file to write smoothed results
        options (ClassificationSmoothingOptions, optional): see 
          ClassificationSmoothingOptions for details.
            
    Returns:
        dict: MegaDetector-results-formatted dict, identical to what's written to
        [output_file] if [output_file] is not None.
    """
    
    ## Input validation
    
    if options is None:
        options = ClassificationSmoothingOptions()
              
    r = _prepare_results_for_smoothing(input_file, options)
    d = r['d']
    other_category_ids = r['other_category_ids']
    classification_descriptions_clean = r['classification_descriptions_clean']
    classification_descriptions = r['classification_descriptions']
    
    
    ## Smoothing
    
    n_other_classifications_changed = 0
    n_other_images_changed = 0
    n_taxonomic_images_changed = 0
    
    n_detections_flipped = 0
    n_images_changed = 0
    n_taxonomic_classification_changes = 0    
        
    # im = d['images'][0]    
    for im in tqdm(d['images']):
        
        r = _smooth_single_image(im,
                                 options,
                                 other_category_ids,
                                 classification_descriptions=classification_descriptions,
                                 classification_descriptions_clean=classification_descriptions_clean)
        
        if r is None:
            continue
        
        n_detections_flipped_this_image = r['n_detections_flipped_this_image']
        n_other_classifications_changed_this_image = \
            r['n_other_classifications_changed_this_image']
        n_taxonomic_changes_this_image = r['n_taxonomic_changes_this_image']
        
        n_detections_flipped += n_detections_flipped_this_image
        n_other_classifications_changed += n_other_classifications_changed_this_image
        n_taxonomic_classification_changes += n_taxonomic_changes_this_image
        
        if n_detections_flipped_this_image > 0:
            n_images_changed += 1
        if n_other_classifications_changed_this_image > 0:
            n_other_images_changed += 1
        if n_taxonomic_changes_this_image > 0:
            n_taxonomic_images_changed += 1
        
    # ...for each image    
    
    print('Classification smoothing: changed {} detections on {} images'.format(
        n_detections_flipped,n_images_changed))
    
    print('"Other" smoothing: changed {} detections on {} images'.format(
          n_other_classifications_changed,n_other_images_changed))
    
    print('Taxonomic smoothing: changed {} detections on {} images'.format(
          n_taxonomic_classification_changes,n_taxonomic_images_changed))
    
        
    ## Write output
    
    if output_file is not None:    
        print('Writing results after image-level smoothing to:\n{}'.format(output_file))
        with open(output_file,'w') as f:
            json.dump(d,f,indent=1)        

    return d

# ...def smooth_classification_results_image_level(...)


#%% Sequence-level smoothing
    
def smooth_classification_results_sequence_level(input_file,
                                                 cct_sequence_information,
                                                 output_file=None,
                                                 options=None):
    """
    Smooth classifications at the sequence level for all results in the MD-formatted results
    file [md_results_file], optionally writing a new set of results to [output_file].
    
    This function generally expresses the notion that a sequence that looks like
    deer/deer/deer/elk/deer/deer/deer/deer is really just a deer.
    
    Args:
        input_file (str or dict): MegaDetector-formatted classification results file to smooth
          (or already-loaded results).  If you supply a dict, it's modified in place by default, but
          a copy can be forced by setting options.modify_in_place=False.
        cct_sequence_information (str, dict, or list): COCO Camera Traps file containing sequence IDs for
          each image (or an already-loaded CCT-formatted dict, or just the 'images' list from a CCT dict).
        output_file (str, optional): .json file to write smoothed results
        options (ClassificationSmoothingOptions, optional): see 
          ClassificationSmoothingOptions for details.
            
    Returns:
        dict: MegaDetector-results-formatted dict, identical to what's written to
        [output_file] if [output_file] is not None.
    """
    
    ## Input validation
    
    if options is None:
        options = ClassificationSmoothingOptions()
    
    r = _prepare_results_for_smoothing(input_file, options)
    d = r['d']
    other_category_ids = r['other_category_ids']
    classification_descriptions_clean = r['classification_descriptions_clean']
    classification_descriptions = r['classification_descriptions']
        
    
    ## Make a list of images appearing in each sequence
    
    if isinstance(cct_sequence_information,list):
        image_info = cct_sequence_information
    elif isinstance(cct_sequence_information,str):
        print('Loading sequence information from {}'.format(cct_sequence_information))
        with open(cct_sequence_information,'r') as f:
            cct_sequence_information = json.load(f)
            image_info = cct_sequence_information['images']
    else:
        assert isinstance(cct_sequence_information,dict)
        image_info = cct_sequence_information['images']
    
    sequence_to_image_filenames = defaultdict(list)
    
    # im = image_info[0]
    for im in tqdm(image_info):
        sequence_to_image_filenames[im['seq_id']].append(im['file_name'])        
    del image_info
    
    image_fn_to_classification_results = {}
    for im in d['images']:
        fn = im['file']
        assert fn not in image_fn_to_classification_results
        image_fn_to_classification_results[fn] = im
            
        
    ## Smoothing
    
    n_other_classifications_changed = 0
    n_other_sequences_changed = 0
    n_taxonomic_sequences_changed = 0
    
    n_detections_flipped = 0
    n_sequences_changed = 0
    n_taxonomic_classification_changes = 0    
        
    # sequence_id = list(sequence_to_image_filenames.keys())[0]
    for sequence_id in sequence_to_image_filenames.keys():

        image_filenames_this_sequence = sequence_to_image_filenames[sequence_id]
        
        detections_this_sequence = []
        for image_filename in image_filenames_this_sequence:
            im = image_fn_to_classification_results[image_filename]
            if 'detections' not in im or im['detections'] is None:
                continue
            detections_this_sequence.extend(im['detections'])
        
        if len(detections_this_sequence) == 0:
            continue
        
        r = _smooth_classifications_for_list_of_detections(
            detections=detections_this_sequence, 
            options=options, 
            other_category_ids=other_category_ids,
            classification_descriptions=classification_descriptions, 
            classification_descriptions_clean=classification_descriptions_clean)
    
        if r is None:
            continue
        
        n_detections_flipped_this_sequence = r['n_detections_flipped_this_image']
        n_other_classifications_changed_this_sequence = \
            r['n_other_classifications_changed_this_image']
        n_taxonomic_changes_this_sequence = r['n_taxonomic_changes_this_image']
        
        n_detections_flipped += n_detections_flipped_this_sequence
        n_other_classifications_changed += n_other_classifications_changed_this_sequence
        n_taxonomic_classification_changes += n_taxonomic_changes_this_sequence
        
        if n_detections_flipped_this_sequence > 0:
            n_sequences_changed += 1
        if n_other_classifications_changed_this_sequence > 0:
            n_other_sequences_changed += 1
        if n_taxonomic_changes_this_sequence > 0:
            n_taxonomic_sequences_changed += 1
    
    # ...for each sequence
    
    print('Classification smoothing: changed {} detections in {} sequences'.format(
        n_detections_flipped,n_sequences_changed))
    
    print('"Other" smoothing: changed {} detections in {} sequences'.format(
          n_other_classifications_changed,n_other_sequences_changed))
    
    print('Taxonomic smoothing: changed {} detections in {} sequences'.format(
          n_taxonomic_classification_changes,n_taxonomic_sequences_changed))
    
    
    ## Write output
    
    if output_file is not None:        
        print('Writing sequence-smoothed classification results to {}'.format(
            output_file))        
        with open(output_file,'w') as f:
            json.dump(d,f,indent=1)
            
    return d

# ...smooth_classification_results_sequence_level(...)
