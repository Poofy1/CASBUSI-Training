import os, pickle
from fastai.vision.all import *
import torch.utils.data as TUD
from tqdm import tqdm
from torch.cuda.amp import GradScaler, autocast
from torch import nn
from archs.save_arch import *
from data.Gen_ITS2CLR_util import *
from torch.optim import Adam
from data.format_data import *
from archs.model_GenSCL import *
env = os.path.dirname(os.path.abspath(__file__))
torch.backends.cudnn.benchmark = True
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
    
    
class Instance_Dataset(TUD.Dataset):
    def __init__(self, bags_dict, selection_mask, transform=None, save_processed=False, warmup=True):
        self.transform = transform
        self.save_processed = save_processed
        self.warmup = warmup 
        self.images = []
        self.final_labels = []

        for bag_id, bag_info in bags_dict.items():
            images = bag_info['images']
            image_labels = bag_info['image_labels']
            bag_label = bag_info['bag_labels'][0]  # Assuming each bag has a single label
            
            bag_id_key = bag_id.item() if isinstance(bag_id, torch.Tensor) else bag_id
            has_mask = bag_id_key in selection_mask
            
            for idx, (img, label) in enumerate(zip(images, image_labels)):
                image_label = None
                
                if not self.warmup:
                    # Only include confident instances (selection_mask) or negative bags or instance labels
                    if label[0] is not None:
                        image_label = label[0]
                    elif has_mask and selection_mask[bag_id_key][0][idx] == 1:
                        if selection_mask[bag_id_key][1][idx] is not None:
                            image_label = selection_mask[bag_id_key][1][idx]
                    elif bag_label == 0:
                        image_label = 0
                else:
                    # Include all data but replace with image_labels if present
                    if label[0] is not None:
                        image_label = label[0]
                    else:
                        image_label = bag_label  # Use bag label if instance label is not present
                
                if image_label is not None:
                    self.images.append(img)
                    self.final_labels.append(image_label)
                    
                    
        print(len(self.final_labels))

    def __getitem__(self, index):
        img_path = self.images[index]
        instance_label = self.final_labels[index]
        
        img = Image.open(img_path).convert("RGB")
        image_data_q = self.transform(img)
        image_data_k = self.transform(img)

        return (image_data_q.half(), image_data_k.half()), instance_label


    def __len__(self):
        return len(self.images)
    
    
def collate_instance(batch):
    batch_data_q = []
    batch_data_k = [] 
    batch_labels = []

    for (image_data_q, image_data_k), bag_label in batch:
        batch_data_q.append(image_data_q)
        batch_data_k.append(image_data_k)
        batch_labels.append(bag_label)

    # Stack the images and labels
    batch_data_q = torch.stack(batch_data_q)
    batch_data_k = torch.stack(batch_data_k)
    batch_labels = torch.tensor(batch_labels, dtype=torch.long)

    return (batch_data_q, batch_data_k), batch_labels


def collate_bag(batch):
    batch_data = []
    batch_bag_labels = []
    batch_instance_labels = []
    batch_ids = []  # List to store bag IDs

    for sample in batch:
        image_data, bag_labels, instance_labels, bag_id = sample  # Updated to unpack four items
        batch_data.append(image_data.half())
        batch_bag_labels.append(bag_labels.half())
        batch_instance_labels.append([label_tensor.half() for label_tensor in instance_labels])
        batch_ids.append(bag_id)

    # Use torch.stack for bag labels to handle multiple labels per bag
    out_bag_labels = torch.stack(batch_bag_labels)

    # Converting to a tensor
    out_ids = torch.tensor(batch_ids, dtype=torch.long)

    return batch_data, out_bag_labels, batch_instance_labels, out_ids



class GenSupConLoss(nn.Module):
    def __init__(self, temperature=0.07, contrast_mode='all', base_temperature=0.07):
        super(GenSupConLoss, self).__init__()
        self.temperature = temperature
        self.contrast_mode = contrast_mode
        self.base_temperature = base_temperature

    def forward(self, features, labels, mapped_anchors):
        '''
        Args:
            features: (anchor_features, contrast_features), each: [N, feat_dim]
            labels: (anchor_labels, contrast_labels) each: [N, num_cls]
            mapped_anchors: [N,], boolean tensor indicating anchor status
        '''
        if self.contrast_mode == 'dynamic':
            # features is a tuple (anchor_features, contrast_features)
            anchor_features_map, contrast_features_map = features
            
            # Ensure mapped_anchors is a boolean tensor for indexing
            anchor_indices = mapped_anchors.nonzero(as_tuple=True)[0]
            contrast_indices = (~mapped_anchors).nonzero(as_tuple=True)[0]
            
            # Now select the features and labels based on anchor and contrast indices
            anchor_features = anchor_features_map[anchor_indices]
            contrast_features = contrast_features_map[contrast_indices]

            anchor_labels = labels[0][anchor_indices].float()
            contrast_labels = labels[1][contrast_indices].float()
            
        elif self.contrast_mode == 'all': # anchor+contrast @ anchor+contrast
            anchor_labels = torch.cat(labels, dim=0).float()
            contrast_labels = anchor_labels
            
            anchor_features = torch.cat(features, dim=0)
            contrast_features = anchor_features
        elif self.contrast_mode == 'one': # anchor @ contrast
            anchor_labels = labels[0].float()
            contrast_labels = labels[1].float()
            
            anchor_features = features[0]
            contrast_features = features[1]
        
        # 1. compute similarities among targets
        anchor_norm = torch.norm(anchor_labels, p=2, dim=-1, keepdim=True) # [anchor_N, 1]
        contrast_norm = torch.norm(contrast_labels, p=2, dim=-1, keepdim=True) # [contrast_N, 1]
        
        deno = torch.mm(anchor_norm, contrast_norm.T)
        mask = torch.mm(anchor_labels, contrast_labels.T) / deno # cosine similarity: [anchor_N, contrast_N]

        logits_mask = torch.ones_like(mask)
        if self.contrast_mode == 'all':
            logits_mask.fill_diagonal_(0)
        mask = mask * logits_mask
        
        # 2. compute logits
        anchor_dot_contrast = torch.div(
            torch.matmul(anchor_features, contrast_features.T),
            self.temperature
        )
        # for numerical stability
        logits_max, _ = torch.max(anchor_dot_contrast, dim=1, keepdim=True)
        logits = anchor_dot_contrast - logits_max.detach()
        # compute log_prob
        exp_logits = torch.exp(logits) * logits_mask
        log_prob = logits - torch.log(exp_logits.sum(1, keepdim=True))
        mean_log_prob_pos = (mask * log_prob).sum(1) / (mask.sum(1) + 1e-8)

        # loss
        loss = - (self.temperature / self.base_temperature) * mean_log_prob_pos
        loss = loss.mean()

        return loss
    


def create_selection_mask(train_bag_logits, val_bag_logits, predictions_included):
    # Combine logits from both training and validation
    combined_logits = []
    original_indices = []
    predictions = []  # To store predictions alongside logits

    # Helper function to add logits, indices, and predictions
    def add_logits_indices_and_predictions(logits_dict, include_predictions=False):
        for bag_id, logits in logits_dict.items():
            for i, logit in enumerate(logits):
                # Interpret confidence as distance from 0.5 (for probabilities)
                confidence = min(logit[0], 1 - logit[0]) if len(logit) == 1 else min(abs(logit - 0.5))
                combined_logits.append(confidence)
                original_indices.append((bag_id, i))
                prediction = None
                if include_predictions:
                    prediction = 0 if logit[0] < 0.5 else 1  # Assuming binary classification
                predictions.append(prediction)

    # Add training and validation logits to the combined list
    add_logits_indices_and_predictions(train_bag_logits, include_predictions=True)
    add_logits_indices_and_predictions(val_bag_logits, include_predictions=True)

    # Rank instances based on their confidence
    top_indices = np.argsort(-np.array(combined_logits))[:predictions_included]

    # Initialize mask and predictions dictionary
    combined_dict = {key: [np.zeros(len(logits), dtype=int), [None]*len(logits)] for key, logits in {**train_bag_logits, **val_bag_logits}.items()}

    # Set mask to 1 for selected top instances and include predictions
    for idx in top_indices:
        original_bag_id, original_position = original_indices[idx]
        combined_dict[original_bag_id][0][original_position] = 1  # Update mask
        combined_dict[original_bag_id][1][original_position] = predictions[idx]  # Update prediction

    return combined_dict



class Args:
    def __init__(self, warm, start_epoch, warm_epochs, learning_rate, lr_decay_rate, num_classes, epochs, warmup_from, cosine, lr_decay_epochs, mix, mix_alpha, KD_temp, KD_alpha, teacher_path, teacher_ckpt):
        self.warm = warm
        self.start_epoch = start_epoch
        self.warm_epochs = warm_epochs
        self.learning_rate = learning_rate
        self.lr_decay_rate = lr_decay_rate
        self.num_classes = num_classes
        self.epochs = epochs
        self.warmup_from = warmup_from
        self.cosine = cosine
        self.lr_decay_epochs = lr_decay_epochs
        self.mix = mix
        self.mix_alpha = mix_alpha
        self.KD_temp = KD_temp
        self.KD_alpha = KD_alpha
        self.teacher_path = teacher_path
        self.teacher_ckpt = teacher_ckpt

if __name__ == '__main__':

    # Config
    model_name = 'Gen_ITS2CLR_2'
    encoder_arch = 'resnet18'
    dataset_name = 'export_01_31_2024'
    label_columns = ['Has_Malignant']
    instance_columns = ['Reject Image', 'Malignant Lesion Present']   #['Reject Image', 'Only Normal Tissue', 'Cyst Lesion Present', 'Benign Lesion Present', 'Malignant Lesion Present']
    img_size = 350
    bag_batch_size = 2
    min_bag_size = 2
    max_bag_size = 10
    instance_batch_size = 8
    model_folder = f"{env}/models/{model_name}/"
    
    #ITS2CLR Config
    feature_extractor_train_count = 5
    initial_ratio = 0 # 0% preditions included
    final_ratio = 0.25 # 25% preditions included
    total_epochs = 500
    warmup_epochs = 10
    
    args = Args(
        warm=True,
        start_epoch=0,
        warm_epochs = warmup_epochs,
        learning_rate=0.01,
        lr_decay_rate=0.1,
        num_classes = 2,
        epochs=50,
        warmup_from=0.001,
        cosine=True,
        lr_decay_epochs=[30, 40],
        mix='mixup',
        mix_alpha=0.2,
        KD_temp=4,
        KD_alpha=0.9,
        teacher_path=model_folder,
        teacher_ckpt='teacher_ITS2CLR.pth'
    )
    
    

    # Paths
    export_location = f'D:/DATA/CASBUSI/exports/{dataset_name}/'
    cropped_images = f"F:/Temp_SSD_Data/{dataset_name}_{img_size}_images/"
    #export_location = '/home/paperspace/cadbusi-LFS/export_09_28_2023/'
    #cropped_images = f"/home/paperspace/Temp_Data/{img_size}_images/"
    

    # Get Training Data
    bags_train, bags_val = prepare_all_data(export_location, label_columns, instance_columns, cropped_images, img_size, min_bag_size, max_bag_size)
    num_labels = len(label_columns)
    
    
    train_transform = T.Compose([
                T.RandomVerticalFlip(),
                T.RandomHorizontalFlip(),
                T.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0),
                T.RandomAffine(degrees=(-45, 45), translate=(0.05, 0.05), scale=(1, 1.2),),
                CLAHETransform(),
                T.ToTensor(),
                GaussianNoise(mean=0, std=0.015), 
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
    
    val_transform = T.Compose([
                CLAHETransform(),
                T.ToTensor(),
                T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])


    # Create datasets
    bag_dataset_train = TUD.Subset(BagOfImagesDataset(bags_train, transform=train_transform, save_processed=False),list(range(0,100)))
    bag_dataset_val = TUD.Subset(BagOfImagesDataset(bags_val, transform=val_transform, save_processed=False),list(range(0,100)))
    #bag_dataset_train = BagOfImagesDataset(bags_train, transform=train_transform, save_processed=False)
    #bag_dataset_val = BagOfImagesDataset(bags_val, transform=val_transform, save_processed=False)
     
    # Create bag data loaders
    bag_dataloader_train = TUD.DataLoader(bag_dataset_train, batch_size=bag_batch_size, collate_fn = collate_bag, drop_last=True, shuffle = True)
    bag_dataloader_val = TUD.DataLoader(bag_dataset_val, batch_size=bag_batch_size, collate_fn = collate_bag, drop_last=True)


    # Get Model
    model = SupConResNet_custom(name=encoder_arch, num_classes=num_labels).cuda()
    total_params = sum(p.numel() for p in model.parameters())
    print(f"Total Parameters: {total_params}")        
        
    optimizer = Adam(model.parameters(), lr=args.learning_rate)
    BCE_loss = nn.BCEWithLogitsLoss()
    genscl = GenSupConLoss(temperature=0.07, contrast_mode='all')
    scaler = GradScaler()
    train_losses_over_epochs = []
    valid_losses_over_epochs = []
    epoch_start = 0
    

    # Check if the model already exists
    model_folder = f"{env}/models/{model_name}/"
    model_path = f"{model_folder}/{model_name}.pth"
    optimizer_path = f"{model_folder}/{model_name}_optimizer.pth"
    stats_path = f"{model_folder}/{model_name}_stats.pkl"
    
    if os.path.exists(model_path):
        model.load_state_dict(torch.load(model_path))
        optimizer.load_state_dict(torch.load(optimizer_path))
        print(f"Loaded pre-existing model from {model_name}")
        
        with open(stats_path, 'rb') as f:
            saved_stats = pickle.load(f)
            train_losses_over_epochs = saved_stats['train_losses']
            valid_losses_over_epochs = saved_stats['valid_losses']
            epoch_start = saved_stats['epoch']
            val_loss_best = saved_stats['val_loss']
    else:
        print(f"{model_name} does not exist, creating new instance")
        os.makedirs(model_folder, exist_ok=True)
        val_loss_best = 99999


    # Training loop
    for epoch in range(epoch_start, total_epochs):
        
        print('Training Default')
        # Training phase
        model.train()
        train_bag_logits = {}
        val_bag_logits = {}
        total_loss = 0.0
        total = 0
        correct = 0

        for (data, yb, instance_yb, id) in tqdm(bag_dataloader_train, total=len(bag_dataloader_train)):
            num_bags = len(data)
            all_images = torch.cat(data, dim=0).cuda()
            yb = yb.cuda()
            optimizer.zero_grad()

            # Process all images and then split per bag
            with autocast():
                features, logits = model(all_images)
                
            split_sizes = [bag.size(0) for bag in data]
            logits_per_bag = torch.split(logits, split_sizes, dim=0)

            batch_loss = 0.0
            for i, (bag_logit, bag_id) in enumerate(zip(logits_per_bag, id)):
                # Find the max sigmoid value for each bag
                bag_max_output = bag_logit.max(dim=0)[0]
                batch_loss += BCE_loss(bag_max_output, yb[i])
                predicted = (bag_max_output > 0.5).float()
                correct += (predicted == yb[i]).sum().item()

                # Store logits for each bag, using id as key
                train_bag_logits[bag_id] = bag_logit.detach().cpu().numpy()


            batch_loss /= num_bags
            total_loss += batch_loss.item() 
            total += num_bags

            scaler.scale(batch_loss).backward()
            scaler.step(optimizer)
            scaler.update()

        train_loss = total_loss / len(bag_dataloader_train)
        train_acc = correct / total

        # Evaluation phase
        model.eval()
        total_val_loss = 0.0
        total = 0
        correct = 0
        all_targs = []
        all_preds = []

        with torch.no_grad():
            for (data, yb, instance_yb, id) in tqdm(bag_dataloader_val, total=len(bag_dataloader_val)):
                num_bags = len(data)
                all_images = torch.cat(data, dim=0).cuda()
                yb = yb.cuda()

                # Process all images and then split per bag
                with autocast():
                    features, logits = model(all_images)
                split_sizes = [bag.size(0) for bag in data]
                logits_per_bag = torch.split(logits, split_sizes, dim=0)

                batch_loss = 0.0
                for i, (bag_logit, bag_id) in enumerate(zip(logits_per_bag, id)):
                    # Find the max sigmoid value for each bag
                    bag_max_output = bag_logit.max(dim=0)[0]
                    batch_loss += BCE_loss(bag_max_output, yb[i])
                    predicted = (bag_max_output > 0.5).float()
                    correct += (predicted == yb[i]).sum().item()

                    # Store logits for each bag during validation, using id as key
                    train_bag_logits[bag_id] = bag_logit.detach().cpu().numpy()

                    all_targs.append(yb[i].item())
                    all_preds.append(predicted.squeeze().tolist())

                batch_loss /= num_bags
                total_val_loss += batch_loss.item()
                total += num_bags

        val_loss = total_val_loss / len(bag_dataloader_val)
        val_acc = correct / total

        train_losses_over_epochs.append(train_loss)
        valid_losses_over_epochs.append(val_loss)
        
        print(f"Epoch {epoch+1} | Acc   | Loss")
        print(f"Train   | {train_acc:.4f} | {train_loss:.4f}")
        print(f"Val     | {val_acc:.4f} | {val_loss:.4f}")
            
            
            
            
            
            
        if True: #val_loss < val_loss_best:
            # Save the model
            val_loss_best = val_loss
            save_state(epoch, label_columns, train_acc, val_loss, val_acc, model_folder, model_name, model, optimizer, all_targs, all_preds, train_losses_over_epochs, valid_losses_over_epochs)
            print("Saved checkpoint due to improved val_loss")
        
        
            
            # Get difficualy ratio
            predictions_ratio = prediction_anchor_scheduler(epoch, total_epochs, warmup_epochs, initial_ratio, final_ratio)
            predictions_included = round(predictions_ratio * instance_batch_size)
            selection_mask = create_selection_mask(train_bag_logits, val_bag_logits, predictions_included)
            
            if epoch < warmup_epochs:
                warmup_on = True
            else:
                warmup_on = False
            
            
            # Used the instance predictions from bag training to update the Instance Dataloader
            #instance_dataset_train = TUD.Subset(Instance_Dataset(bags_train, selection_mask, transform=train_transform, save_processed=False, warmup=warmup_on),list(range(0,100)))
            instance_dataset_train = Instance_Dataset(bags_train, selection_mask, transform=train_transform, save_processed=False, warmup=warmup_on)
            instance_dataloader_train = TUD.DataLoader(instance_dataset_train, batch_size=instance_batch_size, collate_fn = collate_instance, drop_last=True, shuffle = True)
            print('Training Feature Extractor')
            print(f'Including Predictions: {predictions_ratio:.2f} ({predictions_included})')
            
            # Generalized Supervised Contrastive Learning phase
            model.train()
            for i in range(feature_extractor_train_count): 
                losses = AverageMeter()
                
                epoch_loss = 0.0
                total_loss = 0

                # Iterate over the training data
                for idx, (images, instance_labels) in enumerate(tqdm(instance_dataloader_train, total=len(instance_dataloader_train))):
                    warmup_learning_rate(args, epoch, idx, len(instance_dataloader_train), optimizer)
                    
                    # Data preparation 
                    bsz = instance_labels.shape[0]
                    im_q, im_k = images
                    im_q = im_q.cuda(non_blocking=True)
                    im_k = im_k.cuda(non_blocking=True)
                    instance_labels = instance_labels.cuda(non_blocking=True)
                    
                    # image-based regularizations (lam 1 = no mixup)
                    im_q, y0a, y0b, lam0 = mix_fn(im_q, instance_labels, args.mix_alpha, args.mix)
                    im_k, y1a, y1b, lam1 = mix_fn(im_k, instance_labels, args.mix_alpha, args.mix)
                    images = torch.cat([im_q, im_k], dim=0)
                    l_q = mix_target(y0a, y0b, lam0, args.num_classes)
                    l_k = mix_target(y1a, y1b, lam1, args.num_classes)

                    # forward
                    optimizer.zero_grad()
                    with autocast():
                        features, pred = model(images)
                    features = F.normalize(features, dim=1)
                    zk, zq = torch.split(features, [bsz, bsz], dim=0)
                    
                    # Get anchors
                    # In warmup use the whole batch and then Only true labels (no model predictions) as anchors 
                    # After warmup the whole batch is an anchor contrasted against the whole batch
                    # Anchors are on Instance level
                    
                    
                    mapped_anchors = None
                    

                    # get loss (no teacher)
                    loss = genscl([zk, zq], [l_q, l_k], mapped_anchors)
                    losses.update(loss.item(), bsz)
                    
                    if features is None:
                        print("features is None")
                    if [l_q, l_k] is None:
                        print("features is None")
                    if loss is None:
                        print("features is None")
                    
                    # backward
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                    
                print(f'Gen_SCL Loss: {losses.avg:.5f}')
                
                