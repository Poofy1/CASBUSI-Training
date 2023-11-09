import os
from PIL import Image
from torchvision import transforms
from fastai.vision.all import *
import torch.utils.data as TUD
import torchvision.transforms.functional as TF
from tqdm import tqdm
import torchvision.transforms as T
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from sklearn.utils import resample
env = os.path.dirname(os.path.abspath(__file__))


class GaussianNoise(object):
    def __init__(self, mean=0., std=0.1):
        self.mean = mean
        self.std = std

    def __call__(self, tensor):
        noise = torch.randn(tensor.size()).mul_(self.std).add_(self.mean)
        return tensor.add_(noise)

class GrayscaleToRGB:
    def __call__(self, img):
        if len(img.getbands()) == 1:  # If image is grayscale
            img = transforms.functional.to_pil_image(np.stack([img] * 3, axis=-1))
        return img

class ResizeAndPad:
    def __init__(self, output_size, fill=0):
        assert isinstance(output_size, int)
        self.output_size = output_size
        self.fill = fill

    def __call__(self, img):
        w, h = img.size
        if h > w:
            new_h, new_w = self.output_size, int(self.output_size * (w / h))
        else:
            new_h, new_w = int(self.output_size * (h / w)), self.output_size
        img = transforms.functional.resize(img, (new_h, new_w))

        diff = self.output_size - new_w if h > w else self.output_size - new_h
        padding = [diff // 2, diff // 2]

        # If the difference is odd, add the extra padding to the end
        if diff % 2 != 0:
            padding[1] += 1

        # Use the padding values for the left/right or top/bottom
        padding = (padding[0], 0, padding[1], 0) if h > w else (0, padding[0], 0, padding[1])
        img = transforms.functional.pad(img, padding, fill=self.fill)
        return img


class BagOfImagesDataset(TUD.Dataset):

    def __init__(self, bags_dict, train=True, save_processed=False):
        self.bags_dict = bags_dict
        self.unique_bag_ids = list(bags_dict.keys())
        self.save_processed = save_processed
        self.train = train
    
        # Normalize
        if train:
            self.tsfms = T.Compose([
                T.RandomVerticalFlip(),
                T.RandomHorizontalFlip(),
                T.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
                T.RandomAffine(
                    degrees=(-45, 45),  # Rotation
                    translate=(0.1, 0.1),  # Translation
                    scale=(1, 1.2),  # Scaling
                ),
                T.ToTensor(),
                GaussianNoise(mean=0, std=0.025),  # Add slight noise
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
        else:
            self.tsfms = T.Compose([
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])

    def __len__(self):
        return len(self.unique_bag_ids)
    
    def __getitem__(self, index):
        actual_id = self.unique_bag_ids[index]
        label, files_this_bag = self.bags_dict[actual_id]
        data = torch.stack([
            self.tsfms(Image.open(fn).convert("RGB")) for fn in files_this_bag
        ])
        data = data.cuda() 
        
        if self.save_processed:
            save_folder = os.path.join(self.env, 'processed_images')  
            os.makedirs(save_folder, exist_ok=True)
            for idx, img_tensor in enumerate(data):
                img_save_path = os.path.join(save_folder, f'bag_{actual_id}_img_{idx}.jpg')
                img = TF.to_pil_image(img_tensor.cpu().detach())
                img.save(img_save_path)
                
        label = torch.tensor(label, dtype=torch.float32)
        
        return data, label, actual_id
    
    def unnormalize(self, tensor):
        mean = torch.tensor([0.485, 0.456, 0.406]).view(-1, 1, 1).to(tensor.device)
        std = torch.tensor([0.229, 0.224, 0.225]).view(-1, 1, 1).to(tensor.device)
        tensor = tensor * std + mean  # unnormalize
        return torch.clamp(tensor, 0, 1)
    
    
    def n_features(self):
        return self.data.size(1)




def filter_raw_data(breast_data, image_data):

    # Join dataframes on PatientID
    data = pd.merge(breast_data, image_data, left_on=['Accession_Number', 'Breast'], right_on=['Accession_Number', 'laterality'], suffixes=('', '_image_data'))

    # Remove columns from image_data that also exist in breast_data
    for col in breast_data.columns:
        if col + '_image_data' in data.columns:
            data.drop(col + '_image_data', axis=1, inplace=True)
    
    
    data = data[data['Has_Unknown'] == False]

    # Reset the index
    data.reset_index(drop=True, inplace=True)
    
    data.to_csv(f'D:/DATA/CASBUSI/exports/export_10_31_2023/testing.csv')
    
    return data


def create_bags(data, min_size, max_size, root_dir):
    unique_patient_ids = data['Accession_Number'].unique()

    bags_dict = {}  # This will be indexed by bag_id

    for patient_id in tqdm(unique_patient_ids):
        patient_data = data[data['Accession_Number'] == patient_id]

        # Exclude bags that are outside the size range
        if not (min_size <= len(patient_data) <= max_size):
            continue

        # Initialize the list of file names for this bag
        bag_files = [os.path.join(root_dir, row['ImageName']) for _, row in patient_data.iterrows()]
        # Assume all labels in the bag are the same, so take the label of the first one
        bag_label = int(patient_data.iloc[0]['Has_Malignant'])

        # Add to dictionary
        bags_dict[patient_id] = [bag_label, bag_files]

    return bags_dict




def count_bag_labels(bags_dict):
    positive_bag_count = 0
    negative_bag_count = 0

    for bag_id, bag_contents in bags_dict.items():
        label = bag_contents[0]  # The label is the first element in the list
        if label == 1:
            positive_bag_count += 1
        else:
            negative_bag_count += 1

    return positive_bag_count, negative_bag_count


def process_single_image(row, root_dir, output_dir, resize_and_pad):
    try:
        img_name = row['ImageName']
        input_path = os.path.join(f'{root_dir}images/', img_name)
        output_path = os.path.join(output_dir, img_name)

        if os.path.exists(output_path):  # Skip images that are already processed
            return

        image = Image.open(input_path)
        image = resize_and_pad(image)
        image.save(output_path)
    except Exception as e:
        print(f"Error processing image {img_name}: {e}")

def preprocess_and_save_images(data, root_dir, output_dir, image_size, fill=0):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    resize_and_pad = ResizeAndPad(image_size, fill)
    data_rows = [row for _, row in data.iterrows()]  # Convert the DataFrame to a list of rows

    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        futures = {executor.submit(process_single_image, row, root_dir, output_dir, resize_and_pad): row for row in data_rows}

        with tqdm(total=len(futures)) as pbar:
            for future in as_completed(futures):
                future.result()  # We don't actually use the result, but this will raise any exceptions
                pbar.update()


def upsample_minority_class(bags_dict, seed=0):
    np.random.seed(seed)  # for reproducibility
    
    # Convert dict to list of tuples for easy processing
    bags_list = [(bag_id, info) for bag_id, info in bags_dict.items()]

    # Separate bags by class
    class_0_bags = [bag for bag in bags_list if bag[1][0] == 0]
    class_1_bags = [bag for bag in bags_list if bag[1][0] == 1]
    
    # Identify the minority class
    minority_bags = class_0_bags if len(class_0_bags) < len(class_1_bags) else class_1_bags
    majority_bags = class_1_bags if len(class_0_bags) < len(class_1_bags) else class_0_bags
    
    # Calculate how many duplicates are needed
    num_to_oversample = len(majority_bags) - len(minority_bags)
    
    # Randomly duplicate minority class bags and assign new unique ids
    oversampled_bags = resample(minority_bags, n_samples=num_to_oversample, random_state=seed)
    max_existing_id = max(bags_dict.keys())
    
    # Add the oversampled bags to the bags_dict with new IDs
    for bag in oversampled_bags:
        max_existing_id += 1  # increment to get a new unique ID
        bags_dict[max_existing_id] = bag[1]

    return bags_dict
    
    
def prepare_all_data(export_location, case_study_data, breast_data, image_data, cropped_images, img_size, min_bag_size, max_bag_size):
    
    print("Preprocessing Data...")
    data = filter_raw_data(breast_data, image_data)
       
    #Cropping images
    preprocess_and_save_images(data, export_location, cropped_images, img_size)
    
    # Split the data into training and validation sets
    train_patient_ids = case_study_data[case_study_data['valid'] == 0]['Patient_ID']
    val_patient_ids = case_study_data[case_study_data['valid'] == 1]['Patient_ID']
    train_data = data[data['Patient_ID'].isin(train_patient_ids)].reset_index(drop=True)
    val_data = data[data['Patient_ID'].isin(val_patient_ids)].reset_index(drop=True)
    
    bags_train = create_bags(train_data, min_bag_size, max_bag_size, cropped_images)
    bags_val = create_bags(val_data, min_bag_size, max_bag_size, cropped_images)
    
    bags_train = upsample_minority_class(bags_train)  # Upsample the minority class in the training set
    
    print(f'There are {len(bags_train)} files in the training data')
    print(f'There are {len(bags_val)} files in the validation data')
    malignant_count, non_malignant_count = count_bag_labels(bags_train)
    print(f"Number of Malignant Bags: {malignant_count}")
    print(f"Number of Non-Malignant Bags: {non_malignant_count}")
    
    return bags_train, bags_val