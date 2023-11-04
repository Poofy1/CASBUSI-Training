import os, pickle
from timm import create_model
from fastai.vision.all import *
import torch.utils.data as TUD
from fastai.vision.learner import _update_first_layer
from tqdm import tqdm
from torch import nn
from training_eval import *
from torch.optim import Adam
from data_prep import *
from model_ABMIL import *
from model_TransMIL import *
env = os.path.dirname(os.path.abspath(__file__))
torch.backends.cudnn.benchmark = True
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')


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
    batch_data = []
    batch_labels = []
    batch_ids = []  # List to store bag IDs

    for sample in batch:
        image_data, label, bag_id = sample
        batch_data.append(image_data)
        batch_labels.append(label)
        batch_ids.append(bag_id)  # Append the bag ID

    out_labels = torch.tensor(batch_labels).cuda()
    out_ids = torch.tensor(batch_ids).cuda()  # Convert bag IDs to a tensor
    
    return batch_data, out_labels, out_ids

class EmbeddingBagModel(nn.Module):
    
    def __init__(self, encoder, aggregator, num_classes=1):
        super(EmbeddingBagModel, self).__init__()
        self.encoder = encoder
        self.aggregator = aggregator
        self.num_classes = num_classes
                    
    def forward(self, input):
        num_bags = len(input) # input = [bag #, image #, channel, height, width]
        
        # Concatenate all bags into a single tensor for batch processing
        all_images = torch.cat(input, dim=0)  # Shape: [Total images in all bags, channel, height, width]
        
        # Calculate the embeddings for all images in one go
        features = self.encoder(all_images)
        features_viewed = features.view(features.size(0), -1, features.size(1))
        
        # Split the embeddings back into per-bag embeddings
        split_sizes = [bag.size(0) for bag in input]
        h_per_bag = torch.split(features_viewed, split_sizes, dim=0)
        
        logits = torch.empty(num_bags, self.num_classes).cuda()
        attention_scores = []
        
        for i, h in enumerate(h_per_bag):
            # Ensure that h_bag has a first dimension of 1 before passing it to the aggregator
            h_bag = h.unsqueeze(0)
            
            # Receive four values from the aggregator
            yhat_bag, _, yhat_ins, att_sc = self.aggregator(h_bag)
            
            logits[i] = yhat_bag
            attention_scores.append(att_sc)
        
        # Now return the logits, the features before the aggregation, and the attention scores
        return logits, features, attention_scores


def generate_pseudo_labels(attention_scores):
    """
    Generate pseudo labels for instances using normalized attention scores.
    Normalization ensures that the attention scores sum up to 1 for each bag, 
    representing a probability distribution over instances.
    """
    pseudo_labels = []
    for bag_attention in attention_scores:
        # Normalize the attention scores to sum to 1 for each bag
        pseudo_labels_bag = bag_attention / bag_attention.sum()
        pseudo_labels.append(pseudo_labels_bag)
    return torch.cat(pseudo_labels, dim=0)

def supervised_contrastive_loss(features, labels, temperature=0.07):
    """
    Compute the supervised contrastive loss between features.
    """
    device = features.device
    batch_size = features.shape[0]

    # Normalize the features to be on the unit sphere
    features = F.normalize(features, p=2, dim=1)

    # Compute the pairwise cosine similarities
    similarity_matrix = torch.matmul(features, features.T)

    # Scale the similarity by the temperature
    similarity_matrix /= temperature

    # Create the mask for positive and negative examples
    labels = labels.contiguous().view(-1, 1)
    if labels.dim() == 1:
        labels = labels.unsqueeze(1)
    mask = torch.eq(labels, labels.T).float().to(device)

    # Subtract the max similarity for numerical stability
    max_similarity = torch.max(similarity_matrix, dim=1, keepdim=True)[0]
    exp_similarity = torch.exp(similarity_matrix - max_similarity)

    # Mask-out self-similarities (diagonal elements)
    logits_mask = torch.scatter(
        torch.ones_like(mask),
        1,
        torch.arange(batch_size).view(-1, 1).to(device),
        0
    )
    masked_exp_similarity = exp_similarity * logits_mask

    # Sum of exp similarities for negative pairs
    neg_exp_sum = torch.sum(masked_exp_similarity, dim=1)

    # Position of positive pairs on the similarity matrix
    pos_exp_similarity = exp_similarity * mask

    # Sum of exp similarities for positive pairs, avoiding the self-similarity
    # We subtract 1 to remove the self-similarity as exp(0) = 1
    pos_exp_sum = torch.sum(pos_exp_similarity, dim=1) - 1

    # Log-sum of negative similarities for each sample
    log_neg_exp_sum = torch.log(neg_exp_sum + 1e-10)

    # Loss for each sample
    loss_per_sample = - torch.log(pos_exp_sum / (neg_exp_sum + 1e-10) + 1e-10)

    # Mean loss over the batch
    loss = loss_per_sample.mean()

    return loss



# Training vars
val_acc_best = -1 

def default_train():
    global val_acc_best
    
    # Training phase
    bagmodel.train()
    total_loss = 0.0
    total_acc = 0
    total = 0
    correct = 0
    for (data, yb, _) in tqdm(train_dl, total=len(train_dl)): 
        xb, yb = data, yb.cuda()
        
        optimizer.zero_grad()
        
        outputs, features, attention_scores = bagmodel(xb)
        outputs = outputs.squeeze(dim=1)

        loss = loss_func(outputs, yb)
        loss.backward()
        optimizer.step()

        total_loss += loss.item() * len(xb)
        predicted = torch.round(outputs).squeeze()
        total += yb.size(0)
        correct += predicted.eq(yb.squeeze()).sum().item()

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
        for (data, yb, _) in tqdm(val_dl, total=len(val_dl)): 
            xb, yb = data, yb.cuda()

            outputs, features, attention_scores = bagmodel(xb)
            outputs = outputs.squeeze(dim=1)
            loss = loss_func(outputs, yb)
            
            total_val_loss += loss.item() * len(xb)
            predicted = torch.round(outputs).squeeze() 
            total += yb.size(0)
            correct += predicted.eq(yb.squeeze()).sum().item()
            
            # Confusion Matrix data
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
        
    # Save the model
    if val_acc > val_acc_best:
        val_acc_best = val_acc  # Update the best validation accuracy
        save_state(epoch, train_acc, val_acc, model_folder, model_name, bagmodel, optimizer, all_targs, all_preds, train_losses_over_epochs, valid_losses_over_epochs)
        print("Saved checkpoint due to improved val_acc")


if __name__ == '__main__':

    # Config
    model_name = 'ITS2CLR_1'
    img_size = 350
    batch_size = 5
    min_bag_size = 3
    max_bag_size = 15
    epochs = 500
    lr = 0.001

    # Paths
    export_location = 'D:/DATA/CASBUSI/exports/export_10_31_2023/'
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
    dataset_train = TUD.Subset(BagOfImagesDataset( files_train, ids_train, labels_train),list(range(0,100)))
    dataset_val = TUD.Subset(BagOfImagesDataset( files_val, ids_val, labels_val),list(range(0,100)))
    #dataset_train = BagOfImagesDataset(files_train, ids_train, labels_train, save_processed=False)
    #dataset_val = BagOfImagesDataset(files_val, ids_val, labels_val, train=False)

            
    # Create data loaders
    train_dl =  TUD.DataLoader(dataset_train, batch_size=batch_size, collate_fn = collate_custom, drop_last=True, shuffle = True)
    val_dl =    TUD.DataLoader(dataset_val, batch_size=batch_size, collate_fn = collate_custom, drop_last=True)


    encoder = create_timm_body('resnet18')
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
        
        with open(stats_path, 'rb') as f:
            saved_stats = pickle.load(f)
            train_losses_over_epochs = saved_stats['train_losses']
            valid_losses_over_epochs = saved_stats['valid_losses']
            epoch_start = saved_stats['epoch']
            val_acc_best = saved_stats.get('val_acc', -1)  # If 'val_acc' does not exist, default to -1
    else:
        print(f"{model_name} does not exist, creating new instance")
        os.makedirs(model_folder, exist_ok=True)
        val_acc_best = -1 
    
    
    # Training loop
    for epoch in range(epoch_start, epochs):
        if epoch % 2 == 0:
            print('Training Default')
            default_train()
        else:
            print('Training Feature Extractor')
            # Supervised Contrastive Learning phase
            # Freeze the aggregator and unfreeze the encoder
            for param in aggregator.parameters():
                param.requires_grad = False
            for param in encoder.parameters():
                param.requires_grad = True

            # Iterate over the training data
            for (data, yb, _) in tqdm(train_dl, total=len(train_dl)): 
                xb, yb = data, yb.cuda()
                optimizer.zero_grad()

                # Forward pass through the encoder only
                outputs = encoder(torch.cat(xb, dim=0))

                # Flatten the feature maps for each image
                features = outputs.view(outputs.size(0), -1)

                # Calculate the correct number of features per bag
                split_sizes = [bag.size(0) for bag in xb]
                features_per_bag = torch.split(features, split_sizes, dim=0)

                # Initialize a list to hold losses for each bag
                losses = []

                # Iterate over each bag's features and corresponding labels
                for bag_features, bag_labels in zip(features_per_bag, yb):
                    # Ensure bag_features is on the same device as labels
                    bag_features = bag_features.to(device)
                    bag_labels = bag_labels.to(device)

                    # Compute the loss for the current bag
                    bag_loss = supervised_contrastive_loss(bag_features, bag_labels)

                    # Store the loss
                    losses.append(bag_loss)

                # Combine losses for all bags
                total_loss = torch.mean(torch.stack(losses))

                # Backward pass
                total_loss.backward()
                optimizer.step()

            # After the contrastive update, remember to unfreeze the aggregator and freeze the encoder
            for param in aggregator.parameters():
                param.requires_grad = True
            for param in encoder.parameters():
                param.requires_grad = False
