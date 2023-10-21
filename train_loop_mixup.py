import os, pickle
from timm import create_model
from fastai.vision.all import *
import torch.utils.data as TUD
from fastai.vision.learner import _update_first_layer
from tqdm import tqdm
import torchvision.transforms as T
from PIL import Image
from torch import from_numpy
from torch import nn
from torch.cuda.amp import autocast, GradScaler
from training_eval import *
from torch.optim import Adam
from data_prep import *
from model_ABMIL import *
from model_TransMIL import *
import torchvision.transforms.functional as TF
env = os.path.dirname(os.path.abspath(__file__))
torch.backends.cudnn.benchmark = True


class BagOfImagesDataset(TUD.Dataset):

    def __init__(self, filenames, ids, labels, train=True):
        self.filenames = filenames
        self.labels = from_numpy(labels)
        self.ids = from_numpy(ids)
        self.train = train
    
        # Normalize
        if train:
            self.tsfms = T.Compose([
                T.RandomVerticalFlip(),
                T.RandomHorizontalFlip(),
                T.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.1),
                T.RandomAffine(
                    degrees=(-20, 20),  # Random rotation between -10 and 10 degrees
                    translate=(0.05, 0.05),  # Slight translation
                    scale=(0.95, 1.05),  # Slight scaling
                ),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
        else:
            self.tsfms = T.Compose([
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])

    def __len__(self):
        return len(torch.unique(self.ids))
    
    def __getitem__(self, index):
        where_id = (self.ids == index).cpu().numpy()
        files_this_bag = self.filenames[where_id]
        data = torch.stack([
            self.tsfms(Image.open(fn).convert("RGB")) for fn in files_this_bag
        ]).cuda()

        labels = self.labels[index]

        return data, labels

    def show_image(self, index, img_index=0):
        # Get the transformed image tensor and label
        data, labels = self.__getitem__(index)

        # Select the specified image from the bag
        img_tensor = data[img_index]

        # If the images were normalized, reverse the normalization
        if self.normalize:
            mean = torch.tensor([0.485, 0.456, 0.406]).to(img_tensor.device)
            std = torch.tensor([0.229, 0.224, 0.225]).to(img_tensor.device)
            img_tensor = img_tensor * std[:, None, None] + mean[:, None, None]  # Unnormalize

        # Convert the image tensor to a PIL Image
        img = TF.to_pil_image(img_tensor.cpu())

        # Display the image and label
        plt.imshow(img)
        plt.title(f'Label: {labels}')
        plt.axis('off')  # Hide the axis
        plt.show()

    
    def n_features(self):
        return self.data.size(1)





def save_state():
    # Save the model
    torch.save(bagmodel.state_dict(), model_path)
    torch.save(optimizer.state_dict(), optimizer_path)
    
    # Save stats
    with open(stats_path, 'wb') as f:
        pickle.dump({
            'train_losses': train_losses_over_epochs,
            'valid_losses': valid_losses_over_epochs,
            'epoch': epoch + 1  # Save the next epoch to start
        }, f)

    # Save the loss graph
    plot_loss(train_losses_over_epochs, valid_losses_over_epochs, f"{model_folder}/{model_name}_loss.png")
    
    # Save the confusion matrix
    vocab = ['not malignant', 'malignant']  # Replace with your actual vocab
    plot_Confusion(all_targs, all_preds, vocab, f"{model_folder}/{model_name}_confusion.png")


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


def collate_custom(batch):
    batch_data = []  # List to store bags (which are themselves lists of images)
    batch_labels = []  # List to store labels corresponding to each bag

    for sample in batch:
        image_data, label = sample
        batch_data.append(image_data)  # Append the list of images for this bag
        batch_labels.append(label)  # Append the label for this bag

    out_labels = torch.tensor(batch_labels).cuda()  # Convert labels to a tensor
    
    return batch_data, out_labels  # batch_data is a list of lists, out_labels is a tensor


class EmbeddingBagModel(nn.Module):
    
    def __init__(self, encoder, aggregator, num_classes=1):
        super(EmbeddingBagModel,self).__init__()
        self.encoder = encoder
        self.aggregator = aggregator
        self.num_classes = num_classes
                    
                
    def forward(self, input):
        num_bags = len(input) # input = [bag #, image #, channel, height, width]
        
        # Concatenate all bags into a single tensor for batch processing
        all_images = torch.cat(input, dim=0)  # Shape: [Total images in all bags, channel, height, width]
        
        # Calculate the embeddings for all images in one go
        h_all = self.encoder(all_images)
        h_all = h_all.view(h_all.size(0), -1, h_all.size(1))
        
        # Split the embeddings back into per-bag embeddings
        split_sizes = [bag.size(0) for bag in input]
        h_per_bag = torch.split(h_all, split_sizes, dim=0)
        
        logits = torch.empty(num_bags, self.num_classes).cuda()
        saliency_maps, yhat_instances, attention_scores = [], [], []
        
        for i, h in enumerate(h_per_bag):
            # Ensure that h_bag has a first dimension of 1 before passing it to the aggregator
            h_bag = h.unsqueeze(0)
            
            # Receive four values from the aggregator
            yhat_bag, sm, yhat_ins, att_sc = self.aggregator(h_bag)
            
            logits[i] = yhat_bag
            saliency_maps.append(sm)
            yhat_instances.append(yhat_ins)
            attention_scores.append(att_sc)
        
        return logits



def split_bag_fixed_size(x, sub_bag_size):
    """Split a bag into smaller bags with sub_bag_size images, filling the last sub-bag if necessary."""
    # Randomly shuffle the images
    indices = torch.randperm(x.size(0))
    x = x[indices]
    
    # Calculate the number of sub-bags
    num_sub_bags = (x.size(0) + sub_bag_size - 1) // sub_bag_size  # Ceiling division
    
    sub_bags = []
    for i in range(num_sub_bags):
        start_idx = i * sub_bag_size
        end_idx = min(start_idx + sub_bag_size, x.size(0))  # Avoid going out of bounds

        # If this is the last sub-bag and it's smaller than sub_bag_size, fill it with random samples
        if i == num_sub_bags - 1 and end_idx - start_idx < sub_bag_size:
            size_diff = sub_bag_size - (end_idx - start_idx)
            padding_indices = torch.randint(low=0, high=x.size(0), size=(size_diff,))
            sub_bag = torch.cat([x[start_idx:end_idx], x[padding_indices]], dim=0)
        else:
            sub_bag = x[start_idx:end_idx]

        sub_bags.append(sub_bag)

    return sub_bags

def mixup_subbags(x, y, alpha, sub_bag_size=4):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1

    batch_size = len(x)
    index = torch.randperm(batch_size)

    mixed_x = []
    for i in range(batch_size):
        # Split bags into sub-bags of fixed size
        sub_bags_x1 = split_bag_fixed_size(x[i], sub_bag_size)
        sub_bags_x2 = split_bag_fixed_size(x[index[i]], sub_bag_size)

        mixed_sub_bags = []
        min_len = min(len(sub_bags_x1), len(sub_bags_x2))  # Get minimum length to avoid index out of range
        for j in range(min_len):
            sub_x1, sub_x2 = sub_bags_x1[j], sub_bags_x2[j]

            # Perform mixup on sub-bags
            mixed_sub_bag = lam * sub_x1 + (1 - lam) * sub_x2
            mixed_sub_bags.append(mixed_sub_bag)

        # Recombine into a single bag
        mixed_x.append(torch.cat(mixed_sub_bags))

    # Mixing up the labels
    y_a, y_b = y, y[index]
    mixed_y = lam * y_a + (1 - lam) * y_b

    return mixed_x, mixed_y, lam, index


if __name__ == '__main__':

    # Config
    model_name = 'Mixup3'
    img_size = 256
    batch_size = 5
    min_bag_size = 3
    max_bag_size = 15
    epochs = 10000
    alpha = .4
    lr = 0.001

    # Paths
    export_location = 'D:/DATA/CASBUSI/exports/export_09_28_2023/'
    cropped_images = f"F:/Temp_SSD_Data/{img_size}_images/"
    #export_location = '/home/paperspace/cadbusi-LFS/export_09_28_2023/'
    #cropped_images = f"/home/paperspace/Temp_Data/{img_size}_images/"
    case_study_data = pd.read_csv(f'{export_location}/CaseStudyData.csv')
    breast_data = pd.read_csv(f'{export_location}/BreastData.csv')
    image_data = pd.read_csv(f'{export_location}/ImageData.csv')
    

    
    files_train, ids_train, labels_train, files_val, ids_val, labels_val = prepare_all_data(export_location, case_study_data, breast_data, image_data, 
                                                                                            cropped_images, img_size, min_bag_size, max_bag_size)



    print("Training Data...")
    # Create datasets
    #dataset_train = TUD.Subset(BagOfImagesDataset( files_train, ids_train, labels_train),list(range(0,100)))
    #dataset_val = TUD.Subset(BagOfImagesDataset( files_val, ids_val, labels_val),list(range(0,100)))
    dataset_train = BagOfImagesDataset(files_train, ids_train, labels_train, train=True)
    dataset_val = BagOfImagesDataset(files_val, ids_val, labels_val, train=False)

        
    # Create data loaders
    train_dl =  TUD.DataLoader(dataset_train, batch_size=batch_size, collate_fn = collate_custom, drop_last=True, shuffle = True)
    val_dl =    TUD.DataLoader(dataset_val, batch_size=batch_size, collate_fn = collate_custom, drop_last=True)


    encoder = create_timm_body('resnet50')
    nf = num_features_model( nn.Sequential(*encoder.children()))
    
    # bag aggregator
    aggregator = ABMIL_aggregate( nf = nf, num_classes = 1, pool_patches = 3, L = 128)
    #aggregator = TransMIL(dim_in=nf, dim_hid=512, n_classes=1)

    # total model
    bagmodel = EmbeddingBagModel(encoder, aggregator).cuda()
    total_params = sum(p.numel() for p in bagmodel.parameters())
    print(f"Total Parameters: {total_params}")
        
        
    optimizer = Adam(bagmodel.parameters(), lr=lr)
    loss_func = nn.BCELoss()
    train_losses_over_epochs = []
    valid_losses_over_epochs = []
    epoch_start = 0
    
    
    # Check if the model already exists
    model_folder = f"{env}/models/{model_name}/"
    model_path = f"{model_folder}/{model_name}.pth"
    optimizer_path = f"{model_folder}/{model_name}_optimizer.pth"
    stats_path = f"{model_folder}/{model_name}_stats.pkl"
    
    if os.path.exists(model_path):
        bagmodel.load_state_dict(torch.load(model_path))
        optimizer.load_state_dict(torch.load(optimizer_path))
        print(f"Loaded pre-existing model from {model_name}")
        
        # Get stat data
        with open(stats_path, 'rb') as f:
            saved_stats = pickle.load(f)
            train_losses_over_epochs = saved_stats['train_losses']
            valid_losses_over_epochs = saved_stats['valid_losses']
            epoch_start = saved_stats['epoch']
    else:
        print(f"{model_name} does not exist, creating new instance")
        os.makedirs(model_folder, exist_ok=True)
    
    
    
    # Training loop
    for epoch in range(epoch_start, epochs):
        # Training phase
        bagmodel.train()
        total_loss = 0.0
        total_acc = 0
        total = 0
        correct = 0
        for (data, yb) in tqdm(train_dl, total=len(train_dl)): 
            xb, yb = data, yb.cuda()
            
            mixed_x, mixed_y, lam, index = mixup_subbags(xb, yb, alpha=alpha, sub_bag_size=3)
            
            optimizer.zero_grad()
            
            outputs = bagmodel(mixed_x).squeeze(dim=1)
            loss = loss_func(outputs, mixed_y)
            
            loss.backward()
            optimizer.step()

            total_loss += loss.item() * len(xb)
            
            # Calculate accuracy taking mixup into account
            with torch.no_grad():
                predicted = torch.round(outputs).squeeze()
                # split mixed labels back into the original labels
                orig_labels_1, orig_labels_2 = yb, yb[index]
                # calculate accuracy for each set of labels
                correct_label_1 = (predicted == orig_labels_1).float()
                correct_label_2 = (predicted == orig_labels_2).float()
                # mixup accuracy: weighted average of the accuracy for each set of labels
                mixup_acc = lam * correct_label_1 + (1 - lam) * correct_label_2

            total += mixed_y.size(0)
            correct += mixup_acc.sum().item()

        train_loss = total_loss / total
        train_acc = correct / total


        # Evaluation phase
        bagmodel.eval()
        total_val_loss = 0.0
        total_val_acc = 0.0
        total = 0
        correct = 0
        all_targs = []
        all_preds = []
        with torch.no_grad():
            for (data, yb) in tqdm(val_dl, total=len(val_dl)): 
                xb, yb = data, yb.cuda()


                outputs = bagmodel(xb).squeeze(dim=1)
                loss = loss_func(outputs, yb)
                
                total_val_loss += loss.item() * len(xb)
                predicted = torch.round(outputs).squeeze() 
                total += yb.size(0)
                correct += predicted.eq(yb.squeeze()).sum().item()
                
                if epoch == epochs - 1 or (epoch + 1) % 10 == 0:
                    all_targs.extend(yb.cpu().numpy())
                    if len(predicted.size()) == 0:
                        predicted = predicted.view(1)
                    all_preds.extend(predicted.cpu().detach().numpy())

        val_loss = total_val_loss / total
        val_acc = correct / total
        
        train_losses_over_epochs.append(train_loss)
        valid_losses_over_epochs.append(val_loss)
        
        print(f"Epoch {epoch+1} | Acc   | Loss")
        print(f"Train   | {train_acc:.4f} | {train_loss:.4f}")
        print(f"Val     | {val_acc:.4f} | {val_loss:.4f}")
        
        # Save the model every x epochs
        if (epoch + 1) % 10 == 0:
            save_state()
            print("Saved checkpoint")
    
    # Save the model
    save_state()