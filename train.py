import logging
import os
import zipfile
import sys
import json
import time
from tqdm import tqdm
import torch
import numpy as np

from sklearn.metrics import average_precision_score
from SoccerNet.Evaluation.utils import AverageMeter, getMetaDataTask
import glob
from utils import evaluate as evaluate_spotting
from SoccerNet.Evaluation.DenseVideoCaptioning import evaluate as evaluate_dvc
import numpy as np
import evaluate
#from nlgeval import NLGEval
from torch.nn.utils.rnn import pack_padded_sequence

import wandb
#nlg-evalが使えなかったためHaggingfaceのメトリクスを用意
#入力は答え(ref: list)、予測生成文(pred: list)
def caption_scorer(ref, pred, device):
    device = f'cuda:{device}'
    metrics = ['bleu', 'rouge', 'meteor', 'bertscore']
    results = {}
    for i in metrics:
        metric = evaluate.load(i)
        if i == 'bertscore':
            result = metric.compute(predictions= pred, references= ref, device=device, model_type="distilbert-base-uncased")
            result = {f'bert_{k}': round(np.mean(result[k]),5) for k in ['precision', 'recall', 'f1']}
            results.update(result)
            continue
        result = metric.compute(predictions= pred, references= ref)
        if i == 'bleu':   
            result = {f'bleu{i+1}': round(result['precisions'][i], 5) for i in range(4)}
            results.update(result)
            continue
        result = {k: round(v,5) for k,v in result.items() if type(v) != list}
        results.update(result)
        
    return results

def trainer(phase, train_loader,
            val_loader,
            val_metric_loader,
            model,
            optimizer,
            scheduler,
            criterion,
            model_name,
            device=0,
            max_epochs=1000,
            evaluation_frequency=20):

    logging.info("Start %s training" % phase)

    best_loss = 9e99

    os.makedirs(os.path.join("models", model_name, phase), exist_ok=True)
    for epoch in range(max_epochs):
        best_model_path = os.path.join("models", model_name, phase, "model.pth.tar")

        # train for one epoch
        loss_training = train(phase, train_loader, model, criterion,
                              optimizer, epoch + 1, device, train=True)

        # evaluate on validation set
        loss_validation = train(phase, val_loader, model, criterion, 
                              optimizer, epoch + 1, device, train=False)

        state = {
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_loss': best_loss,
            'optimizer': optimizer.state_dict(),
        }
        os.makedirs(os.path.join("models", model_name), exist_ok=True)

        # remember best prec@1 and save checkpoint
        is_better = loss_validation < best_loss
        best_loss = min(loss_validation, best_loss)

        # Save the best model based on loss only if the evaluation frequency too long
        if is_better:
            torch.save(state, best_model_path)

        # Test the model on the validation set
        if epoch % evaluation_frequency == 0 and epoch != 0:
            if phase == "caption":
                test = validate_captioning
            elif phase == "spotting":
                test = validate_spotting
            elif phase == "classifying":
                test = validate_classifying
            # test = validate_captioning if phase == "caption" else validate_spotting
            performance_validation = test(
                val_metric_loader,
                model,
                model_name, device)

            logging.info("Validation performance at epoch " +
                         str(epoch+1) + " -> " + str(performance_validation))
            
            wandb.log({**{
                f"loss_train_{phase}": loss_training,
                f"loss_val_{phase}": loss_validation,
                "epoch" : epoch,
                }, **{f"{k}_val" : v for k, v in performance_validation.items()}} )
            torch.save(state, os.path.join("models", model_name, phase, f"model_{epoch}.pth.tar"))
        else:
            wandb.log({
                f"loss_train_{phase}": loss_training,
                f"loss_val_{phase}": loss_validation,
                "epoch" : epoch,
                })

        # Reduce LR on Plateau after patience reached
        # prevLR = optimizer.param_groups[0]['lr']
        # scheduler.step(loss_validation)
        # currLR = optimizer.param_groups[0]['lr']
        # if (currLR is not prevLR and scheduler.num_bad_epochs == 0):
        #     logging.info("Plateau Reached!")

        # if (prevLR < 2 * scheduler.eps and
        #         scheduler.num_bad_epochs >= scheduler.patience):
        #     logging.info(
        #         "Plateau Reached and no more reduction -> Exiting Loop")
        #     break
        scheduler.step()

    return

def train(phase, dataloader, model, criterion, optimizer, epoch, device=0, train=False):

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    
    # switch to train mode
    if train:
        model.train()
        
    else:
        model.eval()

    end = time.time()
    with tqdm(enumerate(dataloader), total=len(dataloader)) as t:
        for i, batch in t:
            # measure data loading time
            data_time.update(time.time() - end)
            if phase == "spotting":
                feats, labels = batch
                feats = feats.cuda(device)
                labels = labels.cuda(device) # Bx2 (0, 7)
                #label[:,1]は0~13(0はコメントなし、1~13はコメントありかつ、ラベル分類)
                labels = labels[:, 1].long().cuda(device)
                # compute output
                output = model(feats) # Bx18
                loss = criterion(output, labels)
            elif phase == "caption":
                (feats, caption), lengths, mask, caption_or, cap_id = batch
                caption = caption.cuda(device)
                target = caption[:, 1:] #remove SOS token
                lengths = lengths - 1
                #pack_padded_sequence to do less computation
                target = pack_padded_sequence(target, lengths, batch_first=True, enforce_sorted=False)[0]
                mask = pack_padded_sequence(mask[:, 1:], lengths, batch_first=True, enforce_sorted=False)[0]
                feats = feats.cuda(device)
                # compute output
                
                output = model(feats, caption, lengths)
                
                loss = criterion(output[mask], target[mask])
                
                
            elif phase == "classifying":
                feats, labels = batch
                feats = feats.cuda(device)
                labels = labels.cuda(device)
                # compute output
                
                output = model(feats)
                
                # hand written NLL criterion
                loss = criterion(output, labels)
            
            else:
                NotImplementedError()
            
            # check if the loss is NaN
            if torch.isnan(loss):  
                logging.error("Loss is NaN")
                print(i)
                logging.info(loss)
                #print(feats, caption, output)
                #logging.info(lengths)
                # print model parameter norms
                # for name, param in model.named_parameters():
                #     logging.info(f"{name} - {param.norm().item()}")
                sys.exit()
            # measure accuracy and record loss
            losses.update(loss.detach().item(), feats.size(0))

            if train:
                # compute gradient and do SGD step
                optimizer.zero_grad()
                loss.backward()
                #clip the gradient to avoid exploding gradient
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if train:
                desc = f'Train {epoch}: '
            else:
                desc = f'Evaluate {epoch}: '
            desc += f'Time {batch_time.avg:.3f}s '
            desc += f'(it:{batch_time.val:.3f}s) '
            desc += f'Data:{data_time.avg:.3f}s '
            desc += f'(it:{data_time.val:.3f}s) '
            desc += f'Loss {losses.avg:.4e} '
            t.set_description(desc)

    return losses.avg

def validate_spotting(dataloader, model, model_name, device=0):
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()

    end = time.time()
    all_labels = []
    all_outputs = []
    with tqdm(enumerate(dataloader), total=len(dataloader)) as t:
        for i, (feats, labels) in t:
            # measure data loading time
            data_time.update(time.time() - end)
            feats = feats.cuda(device)
            
            labels = (labels > 0) * 1.0
            all_labels.append(labels.detach().numpy())

            # compute output
            output = model(feats)
            output = torch.nn.functional.softmax(output, dim=1) # Bx18
            output = torch.stack([output[:, 0], 1-output[:, 0]], dim=1)
            all_outputs.append(output.cpu().detach().numpy())

            batch_time.update(time.time() - end)
            end = time.time()

            desc = f'Test (cls): '
            desc += f'Time {batch_time.avg:.3f}s '
            desc += f'(it:{batch_time.val:.3f}s) '
            desc += f'Data:{data_time.avg:.3f}s '
            desc += f'(it:{data_time.val:.3f}s) '
            t.set_description(desc)

    AP = []
    for i in range(1, dataloader.dataset.num_classes+1):
        AP.append(average_precision_score(np.concatenate(all_labels)
                                          [:, i], np.concatenate(all_outputs)[:, i]))

    mAP = np.mean(AP)

    return {"mAP-sklearn" : mAP}

def validate_classifying(dataloader, model, model_name, device=0):
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()

    end = time.time()
    correct_predictions = 0.0
    total_predictions = 0.0
    with torch.no_grad():
        with tqdm(enumerate(dataloader), total=len(dataloader)) as t:
            for i, (feats, labels) in t:
                # measure data loading time
                data_time.update(time.time() - end)
                feats = feats.cuda(device)

                # compute output
                output = model(feats)

                # all_labels.append(labels.detach().numpy())
                # all_outputs.append(output.cpu().detach().numpy())
                _, predicted = torch.max(output.data, 1)
                total_predictions += labels.size(0)
                correct_predictions += (predicted.cpu().detach() == labels).sum().item()


                batch_time.update(time.time() - end)
                end = time.time()

                desc = f'Test (cls): '
                desc += f'Time {batch_time.avg:.3f}s '
                desc += f'(it:{batch_time.val:.3f}s) '
                desc += f'Data:{data_time.avg:.3f}s '
                desc += f'(it:{data_time.val:.3f}s) '
                t.set_description(desc)

    return {"accuracy" : correct_predictions/total_predictions}

def test_spotting(dataloader, model, model_name, save_predictions=True, NMS_window=30, NMS_threshold=0.5, device=0):
    
    split = '_'.join(dataloader.dataset.split)
    output_folder = f"outputs/{split}"
    output_results = os.path.join("models", model_name, output_folder)
    

    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()

    _, _, _, inv_dict = getMetaDataTask("caption", "SoccerNet", dataloader.dataset.version)

    end = time.time()
    with tqdm(enumerate(dataloader), total=len(dataloader)) as t:
        for i, (game_ID, feat_half1, feat_half2, label_half1, label_half2) in t:
            data_time.update(time.time() - end)

            # Batch size of 1
            game_ID = game_ID[0]
            feat_half1 = feat_half1.squeeze(0)
            label_half1 = label_half1.float().squeeze(0)
            feat_half2 = feat_half2.squeeze(0)
            label_half2 = label_half2.float().squeeze(0)

            # Compute the output for batches of frames
            BS = 256
            timestamp_long_half_1 = []
            for b in range(int(np.ceil(len(feat_half1)/BS))):
                start_frame = BS*b
                end_frame = BS*(b+1) if BS * \
                    (b+1) < len(feat_half1) else len(feat_half1)
                feat = feat_half1[start_frame:end_frame].cuda(device)
                # output = model(feat)
                # output = torch.nn.functional.softmax(output, dim=1)
                output = model(feat)
                output = torch.nn.functional.softmax(output, dim=1) # Bx14
                #output = torch.stack([output[:, 0], 1-output[:, 0]], dim=1)

                max_ind = torch.argmax(output[:, 1:], dim=1)+1 
                #max_ind = (batch, 1) (+1をする意味は[1:]した瞬間にindexが1つ下がるから)
                output = torch.stack([output[:, 0], output[torch.arange(output.size(0)), max_ind]], dim=1)
                #output = (コメント無しの確率、コメントありかつ最も確信度が高いラベルの確率)
                output = output.cpu().detach().numpy()
                timestamp_long_half_1.append(output)
            timestamp_long_half_1 = np.concatenate(timestamp_long_half_1)

            timestamp_long_half_2 = []
            for b in range(int(np.ceil(len(feat_half2)/BS))):
                start_frame = BS*b
                end_frame = BS*(b+1) if BS * \
                    (b+1) < len(feat_half2) else len(feat_half2)
                feat = feat_half2[start_frame:end_frame].cuda(device)
                output = model(feat)
                output = torch.nn.functional.softmax(output, dim=1)
                #output = torch.stack([output[:, 0], 1-output[:, 0]], dim=1)

                max_ind = torch.argmax(output[:, 1:], dim=1)+1 # Bx1
                output = torch.stack([output[:, 0], output[torch.arange(output.size(0)), max_ind]], dim=1)

                output = output.cpu().detach().numpy()
                timestamp_long_half_2.append(output)
            timestamp_long_half_2 = np.concatenate(timestamp_long_half_2)


            timestamp_long_half_1 = timestamp_long_half_1[:, 1:] #最も確信度が高いラベルのみ(それでも0.3とかありえる)
            timestamp_long_half_2 = timestamp_long_half_2[:, 1:] # Bx1 

            batch_time.update(time.time() - end)
            end = time.time()

            desc = f'Test (spot.): '
            desc += f'Time {batch_time.avg:.3f}s '
            desc += f'(it:{batch_time.val:.3f}s) '
            desc += f'Data:{data_time.avg:.3f}s '
            desc += f'(it:{data_time.val:.3f}s) '
            t.set_description(desc)



            def get_spot_from_NMS(Input, window=60, thresh=0.0):

                detections_tmp = np.copy(Input)
                indexes = []
                MaxValues = []
                while(np.max(detections_tmp) >= thresh):

                    # Get the max remaining index and value
                    max_value = np.max(detections_tmp)
                    max_index = np.argmax(detections_tmp)
                    MaxValues.append(max_value)
                    indexes.append(max_index)
                    # detections_NMS[max_index,i] = max_value

                    nms_from = int(np.maximum(-(window/2)+max_index,0))
                    nms_to = int(np.minimum(max_index+int(window/2), len(detections_tmp)))
                    detections_tmp[nms_from:nms_to] = -1

                return np.transpose([indexes, MaxValues]) 
                #転置、すなわち[[index_1, maxvalue_1],[index_2, maxvalue_2],...]

            framerate = dataloader.dataset.framerate
            get_spot = get_spot_from_NMS

            json_data = dict()
            json_data["UrlLocal"] = game_ID
            json_data["predictions"] = list()

            for half, timestamp in enumerate([timestamp_long_half_1, timestamp_long_half_2]):
                #timestamp_longは(batch, 最も確信度の高いラベルの確率)
                for l in range(dataloader.dataset.num_classes):#l=0のみ
                    spots = get_spot(
                        timestamp[:, l], window=NMS_window*framerate, thresh=NMS_threshold) # l = 0 which is out[:, 1:][:, 0]
                    for spot in spots:
                        # print("spot", int(spot[0]), spot[1], spot)
                        frame_index = int(spot[0])
                        confidence = spot[1]
                        # confidence = predictions_half_1[frame_index, l]

                        seconds = int((frame_index//framerate)%60)
                        minutes = int((frame_index//framerate)//60)

                        prediction_data = dict()
                        prediction_data["gameTime"] = f'{half+1} - {int(minutes):02d}:{int(seconds):02d}'
                        prediction_data["label"] = inv_dict[l] #inv_dict = {0: 'comments'}

                        prediction_data["position"] = str(int((frame_index/framerate)*1000))
                        prediction_data["half"] = str(half+1)
                        prediction_data["confidence"] = str(confidence)
                        json_data["predictions"].append(prediction_data)
            
            json_data["predictions"] = sorted(json_data["predictions"], key=lambda x: (int(x["half"]), int(x["position"])))
            if save_predictions:
                os.makedirs(os.path.join("models", model_name, output_folder, game_ID), exist_ok=True)
                with open(os.path.join("models", model_name, output_folder, game_ID, "results_spotting.json"), 'w') as output_file:
                    json.dump(json_data, output_file, indent=4)

    if split == "challenge": 
        print("Visit eval.ai to evalaute performances on Challenge set")
        return None
    
    tight = evaluate_spotting(SoccerNet_path=dataloader.dataset.path, 
                Predictions_path=output_results,
                split=dataloader.dataset.split,
                prediction_file="results_spotting.json", 
                version=dataloader.dataset.version, 
                framerate=dataloader.dataset.framerate, metric="tight")
    
    loose = evaluate_spotting(SoccerNet_path=dataloader.dataset.path, 
                Predictions_path=output_results,
                split=dataloader.dataset.split,
                prediction_file="results_spotting.json", 
                version=dataloader.dataset.version, 
                framerate=dataloader.dataset.framerate, metric="loose")
    
    medium = evaluate_spotting(SoccerNet_path=dataloader.dataset.path, 
                Predictions_path=output_results,
                split=dataloader.dataset.split,
                prediction_file="results_spotting.json", 
                version=dataloader.dataset.version, 
                framerate=dataloader.dataset.framerate, metric="medium")

    tight = {f"{k}_tight" : v for k, v in tight.items() if v!= None}
    loose = {f"{k}_loose" : v for k, v in loose.items() if v!= None}
    medium = {f"{k}_medium" : v for k, v in medium.items() if v!= None}

    results = {**tight, **loose, **medium}

    return results

@torch.no_grad()
def validate_captioning(dataloader, model, model_name, device=0, model_type = 'gpt'):
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()

    end = time.time()
    all_labels = []
    all_outputs = []
    
    with tqdm(dataloader) as t:
        for (feats, caption), lengths, mask, caption_or, cap_id in t:
            # measure data loading time
            data_time.update(time.time() - end)
            feats = feats.cuda(device)
            #print(list(model.sample(feats[0]).detach().cpu()))
            #compute output string
            output = [dataloader.dataset.detokenize(list(model.sample(feats[idx]).detach().cpu()), model_type= model_type) for idx in range(feats.shape[0])]
            
            #haggingfaceのEvaluationを使用する場合、
            #ref(label)はappend(すなわち[[label1],[lebel2],...] にすべきらしい)
            #一方pred(output)は[output1, output2,...] にすべきらしい
            
            all_outputs.append(output[0])
            all_labels.append(caption_or)
            # print("Output:", output[0])
            # print("Labels:", caption_or[0])
            batch_time.update(time.time() - end)
            end = time.time()

            desc = f'Test (cap): '
            desc += f'Time {batch_time.avg:.3f}s '
            desc += f'(it:{batch_time.val:.3f}s) '
            desc += f'Data:{data_time.avg:.3f}s '
            desc += f'(it:{data_time.val:.3f}s) '
            t.set_description(desc)

    scores = caption_scorer(ref= all_labels, pred=all_outputs, device=device)
    return scores

@torch.no_grad()
def test_captioning(dataloader, model, model_name, output_filename = "results_dense_captioning.json", 
                    input_filename="results_spotting.json", device=0, model_type = 'lstm'):
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()

    end = time.time()
    all_outputs = []
    all_index = []

    split = '_'.join(dataloader.dataset.split)
    output_folder = f"outputs/{split}"
    output_results = os.path.join("models", model_name, f"results_dense_captioning_{split}.zip")

    with tqdm(dataloader) as t:
        for feats, game_id, cap_id in t:
            # measure data loading time
            data_time.update(time.time() - end)
            feats = feats.cuda(device)
            output = [dataloader.dataset.detokenize(list(model.sample(feats[idx]).detach().cpu()), model_type= model_type) for idx in range(feats.shape[0])]
            
            all_outputs.extend(output)
            all_index.extend([(i.item(), j.item()) for i, j in zip(game_id, cap_id)])

            batch_time.update(time.time() - end)
            end = time.time()

            desc = f'Test (dense_caption): '
            desc += f'Time {batch_time.avg:.3f}s '
            desc += f'(it:{batch_time.val:.3f}s) '
            desc += f'Data:{data_time.avg:.3f}s '
            desc += f'(it:{data_time.val:.3f}s) '
            t.set_description(desc)
    
    #store output
    captions = dict(zip(all_index, all_outputs))
    for game_id, game in enumerate(dataloader.dataset.listGames):
        path = os.path.join("models", model_name, output_folder, game, input_filename)
        with open(path, 'r') as pred_file:
            preds = json.load(pred_file)
        for caption_id, annotation in enumerate(preds["predictions"]):
            annotation["comment"] = captions[game_id, caption_id]
        with open(os.path.join("models", model_name, output_folder, game, output_filename), 'w') as output_file:
                    json.dump(preds, output_file, indent=4)
    
    def zipResults(zip_path, target_dir, filename="results_spotting.json"):            
            zipobj = zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED)
            rootlen = len(target_dir) + 1
            for base, dirs, files in os.walk(target_dir):
                for file in files:
                    if file == filename:
                        fn = os.path.join(base, file)
                        zipobj.write(fn, fn[rootlen:])
    
    zipResults(zip_path = output_results,
            target_dir = os.path.join("models", model_name, output_folder),
            filename=output_filename)

    if split == "challenge": 
        print("Visit eval.ai to evalaute performances on Challenge set")
        return None
    
    tight = evaluate_dvc(SoccerNet_path=dataloader.dataset.path, Predictions_path=output_results, split=dataloader.dataset.split, version=dataloader.dataset.version, prediction_file=output_filename, window_size=5, include_SODA=False)
    loose = evaluate_dvc(SoccerNet_path=dataloader.dataset.path, Predictions_path=output_results, split=dataloader.dataset.split, version=dataloader.dataset.version, prediction_file=output_filename, window_size=30, include_SODA=False)

    results = {**{f"{k}_tight" : v for k, v in tight.items()}, **{f"{k}_loose" : v for k, v in loose.items()}}

    return results