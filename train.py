import os
from timm import create_model
from fastai.vision.all import *
from torch.utils.data import Dataset, Subset
import matplotlib.pyplot as plt
from fastai.vision.learner import _update_first_layer
from tqdm import tqdm
import torchvision.transforms as T
from PIL import Image
from torchvision import transforms
from torch import from_numpy
from torch import nn
from training_eval import *
from data_prep import *
env = os.path.dirname(os.path.abspath(__file__))



class BagOfImagesDataset(Dataset):

    def __init__(self, data, imsize, normalize=True):
        self.bags = data
        self.normalize = normalize
        self.imsize = imsize

        # Normalize
        if normalize:
            self.tsfms = T.Compose([
                T.ToTensor(),
                T.Resize((self.imsize, self.imsize)),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
        else:
            self.tsfms = T.Compose([
                T.ToTensor(),
                T.Resize((self.imsize, self.imsize))
            ])

    def __len__(self):
        return len(self.bags)

    def __getitem__(self, index):
        bag = self.bags[index]
        filenames = bag[0]
        labels = bag[1]
        ids = bag[2]
        
        data = torch.stack([
            self.tsfms(Image.open(fn).convert("RGB")) for fn in filenames
        ]).cuda()
        
        # Use the first label from the bag labels (assuming all instances in the bag have the same label)
        label = torch.tensor(labels[0], dtype=torch.long).cuda()
        
        # Convert bag ids to tensor and use the first id (assuming all instances have the same id)
        bagid = torch.tensor(ids[0], dtype=torch.long).cuda()

        return data, bagid, label


def collate_custom(batch):
    batch_data = []
    batch_bagids = []
    batch_labels = []
  
    for sample in batch:
        batch_data.append(sample[0])
        batch_bagids.append(sample[1])
        batch_labels.append(sample[2])
  
    out_data = torch.cat(batch_data, dim = 0).cuda()
    out_bagids = torch.cat(batch_bagids).cuda()
    out_labels = torch.stack(batch_labels).cuda()
  
    return (out_data, out_bagids), out_labels



# this function is used to cut off the head of a pretrained timm model and return the body
def create_timm_body(arch:str, pretrained=True, cut=None, n_in=3):
    "Creates a body from any model in the `timm` library."
    model = create_model(arch, pretrained=pretrained, num_classes=0, global_pool='')
    _update_first_layer(model, n_in, pretrained)
    if cut is None:
        ll = list(enumerate(model.children()))
        cut = next(i for i,o in reversed(ll) if has_pool_type(o))
    if isinstance(cut, int): return nn.Sequential(*list(model.children())[:cut])
    elif callable(cut): return cut(model)
    else: raise NameError("cut must be either integer or function")



class IlseBagModel(nn.Module):
    
    def __init__(self, arch, num_classes = 2, pool_patches = 3, pretrained = True):
        super(IlseBagModel,self).__init__()
        self.pool_patches = pool_patches # how many patches to use in predicting instance label
        self.backbone = create_timm_body(arch, pretrained = pretrained)
        self.nf = num_features_model( nn.Sequential(*self.backbone.children()))
        self.num_classes = num_classes # two for binary classification
        self.M = self.nf # is 512 for resnet34
        self.L = 128 # number of latent features in gated attention     
        
        self.saliency_layer = nn.Sequential(        
            nn.Conv2d( self.nf, self.num_classes, (1,1), bias = False),
            nn.Sigmoid() )
        
        self.attention_V = nn.Sequential(
            nn.Linear(self.M, self.L),
            nn.Tanh()
        )

        self.attention_U = nn.Sequential(
            nn.Linear(self.M, self.L),
            nn.Sigmoid()
        )

        self.attention_W = nn.Linear(self.L, self.num_classes)
    
                    
    def forward(self, x, ids):
        # Reshape input to merge the bag and image dimensions
        x_reshaped = x.view(-1, 3, 256, 256)
        h = self.backbone(x_reshaped)
        
        # add attention head to compute instance saliency map and instance labels (as logits)
    
        self.saliency_map = self.saliency_layer(h) # compute activation maps
        map_flatten = self.saliency_map.flatten(start_dim = -2, end_dim = -1)
        selected_area = map_flatten.topk(self.pool_patches, dim=2)[0]
        self.yhat_instance = selected_area.mean(dim=2).squeeze()
        
        # max pool the feature maps to generate feature vector v of length self.nf (number of features)
        v = torch.max( h, dim = 2).values
        v = torch.max( v, dim = 2).values # maxpool complete
        
        # gated-attention
        A_V = self.attention_V(v) 
        A_U = self.attention_U(v) 
        A  = self.attention_W(A_V * A_U)
        
        unique = torch.unique_consecutive(ids)
        yhat_bags = torch.empty(len(unique),self.num_classes).cuda()
        for i,bag in enumerate(unique):
            mask = torch.where(ids == bag)[0]
            A[mask] = nn.functional.softmax( A[mask] , dim = 0 )
            yhat = self.yhat_instance[mask]
            yhat_bags[i] = ( A[mask] * yhat ).sum(dim=0)
        
        self.attn_scores = A
        return yhat_bags
    
# "The regularization term |A| is basically model.saliency_maps.mean()" -from github repo
class L1RegCallback(Callback):
    def __init__(self, reglambda = 0.0001):
        self.reglambda = reglambda
       
    def after_loss(self):
        self.learn.loss += self.reglambda * self.learn.model.saliency_map.mean()

def count_malignant_bags(bags):
    malignant_count = 0
    non_malignant_count = 0
    
    for bag in bags:
        bag_labels = bag[1]  # Extracting labels from the bag
        if sum(bag_labels) > 0:  # If there's even one malignant instance
            malignant_count += 1
        else:
            non_malignant_count += 1
    
    return malignant_count, non_malignant_count



if __name__ == '__main__':

    model_name = 'test1'
    img_size = 256
    batch_size = 10
    bag_size = 20
    epochs = 2
    reg_lambda = 0 #0.001
    lr = 0.0008

    # Load CSV data
    export_location = 'F:/Temp_SSD_Data/export_09_14_2023/'
    case_study_data = pd.read_csv(f'{export_location}/CaseStudyData.csv')
    breast_data = pd.read_csv(f'{export_location}/BreastData.csv')
    image_data = pd.read_csv(f'{export_location}/ImageData.csv')
    data = filter_raw_data(breast_data, image_data)

    #Preparing data
    cropped_images = f"{export_location}/temp_cropped/"
    #preprocess_and_save_images(data, export_location, cropped_images, img_size)

    # Split the data into training and validation sets
    train_patient_ids = case_study_data[case_study_data['valid'] == 0]['Patient_ID']
    val_patient_ids = case_study_data[case_study_data['valid'] == 1]['Patient_ID']
    train_data = data[data['Patient_ID'].isin(train_patient_ids)].reset_index(drop=True)
    val_data = data[data['Patient_ID'].isin(val_patient_ids)].reset_index(drop=True)

    train_bags = create_bags(train_data, bag_size, cropped_images)
    val_bags = create_bags(val_data, bag_size, cropped_images) 
    
    print(f'There are {len(train_data)} files in the training data')
    print(f'There are {len(val_data)} files in the validation data')
    malignant_count, non_malignant_count = count_malignant_bags(train_bags)
    print(f"Number of Malignant Bags: {malignant_count}")
    print(f"Number of Non-Malignant Bags: {non_malignant_count}")


    # Choose the indices you want to use for the subset
    train_indices = list(range(0, 500))  # Adjust the range to your needs
    val_indices = list(range(0, 100))  # Adjust the range to your needs

    # Create datasets
    dataset_train = BagOfImagesDataset( train_bags,img_size)
    dataset_val = BagOfImagesDataset( val_bags,img_size)
        
    # Create data loaders
    train_dl =  DataLoader(dataset_train, batch_size=batch_size, collate_fn = collate_custom, drop_last=True, shuffle = True)
    val_dl =    DataLoader(dataset_val, batch_size=batch_size, collate_fn = collate_custom, drop_last=True)

    # wrap into fastai Dataloaders
    dls = DataLoaders(train_dl, val_dl)


    timm_arch = 'resnet18' #resnet34
    bagmodel = IlseBagModel(timm_arch, pretrained = True).cuda()
    
    learn = Learner(dls, bagmodel, loss_func=CrossEntropyLossFlat(), metrics = accuracy, cbs = L1RegCallback(reg_lambda) )

    # find a good learning rate using mini-batches
    #learn.lr_find()

    learn.fit_one_cycle(epochs,lr)
    
    learn.save(f"{env}/models/{model_name}")
    
    # Save the loss graph
    plot_and_save_training_validation_loss(learn, f"{env}/models/{model_name}_loss.png")
    
    # Save the confusion matrix
    vocab = ['not malignant', 'malignant']  # Replace with your actual vocab
    plot_and_save_confusion_matrix(learn, vocab, f"{env}/models/{model_name}_confusion.png")

        
    