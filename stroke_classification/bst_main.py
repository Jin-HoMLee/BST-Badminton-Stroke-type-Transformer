import torch
from torch import Tensor, nn, optim
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torcheval.metrics.functional import multiclass_f1_score

from transformers import get_cosine_schedule_with_warmup

import pandas as pd
from pathlib import Path
from copy import deepcopy
from collections import namedtuple
import time
from datetime import timedelta

from dataset import get_bone_pairs, prepare_npy_collated_loaders, \
                    RandomTranslation_batch, Dataset_npy
from bst import BST, BST_2, BST_3, BST_3_2, BST_4, \
                BST_5, BST_5_2, BST_5_3, BST_5_4, \
                BST_5_5, BST_5_6, BST_5_7, BST_5_8, \
                BST_5_9, \
                BST_6, BST_6_2, BST_7, BST_7_2, BST_7_3, \
                BST_8, BST_8_2
from utils import show_f1_results, plot_confusion_matrix


Hyp = namedtuple('Hyp', [
    'n_epochs', 'batch_size', 'lr', 'warm_up_step',
    'n_classes', 'seq_len', 'early_stop_n_epochs'
])
hyp = Hyp(
    n_epochs=1600,
    early_stop_n_epochs=300,
    batch_size=128,
    lr=5e-4,
    warm_up_step=400,
    n_classes=35,
    seq_len=100
)


def train_one_epoch(
    model: nn.Module,
    loader,
    random_shift_fn,
    n_bones: int,
    loss_fn,
    optimizer: optim.Optimizer,
    scheduler: optim.lr_scheduler.LambdaLR,
    device
):
    model.train()
    total_loss = 0.0

    for (human_pose, pos, shuttle), video_len, labels in loader:
        human_pose: Tensor = human_pose.to(device)
        shuttle: Tensor = shuttle.to(device)
        pos: Tensor = pos.to(device)
        video_len: Tensor = video_len.to(device)
        labels: Tensor = labels.to(device)

        if n_bones == 0:
            human_pose = random_shift_fn(human_pose)
        else:
            joints = human_pose[:, :, :, :-n_bones, :].contiguous()
            bones = human_pose[:, :, :, -n_bones:, :]

            joints = random_shift_fn(joints)
            human_pose = torch.cat([joints, bones], dim=-2)

        human_pose = human_pose.view(*human_pose.shape[:-2], -1)
        logits = model(human_pose, shuttle, pos, video_len)
        loss: Tensor = loss_fn(logits, labels)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        total_loss += loss.item()
    
    train_loss = total_loss / len(loader)
    return train_loss


@torch.no_grad()
def validate(
    model: nn.Module,
    loss_fn,
    loader,
    device
):
    model.eval()
    total_loss = 0.0
    cum_tp = torch.zeros(hyp.n_classes)
    cum_tn = torch.zeros(hyp.n_classes)
    cum_fp = torch.zeros(hyp.n_classes)
    cum_fn = torch.zeros(hyp.n_classes)

    for (human_pose, pos, shuttle), video_len, labels in loader:
        human_pose: Tensor = human_pose.to(device)
        shuttle: Tensor = shuttle.to(device)
        pos: Tensor = pos.to(device)
        video_len: Tensor = video_len.to(device)
        labels: Tensor = labels.to(device)

        human_pose = human_pose.view(*human_pose.shape[:-2], -1)  # for faster dataset version
        logits = model(human_pose, shuttle, pos, video_len)
        loss: Tensor = loss_fn(logits, labels)
        total_loss += loss.item()

        pred = F.one_hot(torch.argmax(logits, dim=1), hyp.n_classes).bool()
        labels_onehot = F.one_hot(labels, hyp.n_classes).bool()

        tp = torch.sum(pred & labels_onehot, dim=0)
        tn = torch.sum(~pred & ~labels_onehot, dim=0)

        fp = torch.sum(pred & ~labels_onehot, dim=0)
        fn = torch.sum(~pred & labels_onehot, dim=0)

        cum_tp += tp.cpu()
        cum_tn += tn.cpu()
        cum_fp += fp.cpu()
        cum_fn += fn.cpu()

    val_loss = total_loss / len(loader)

    precision = cum_tp / (cum_tp + cum_fp)
    recall = cum_tp / (cum_tp + cum_fn)

    f1_score = 2 * precision * recall / (precision + recall)
    f1_score[f1_score.isnan()] = 0

    f1_score_avg = f1_score.mean()
    f1_score_min = f1_score.min()
    return val_loss, f1_score_avg, f1_score_min


@torch.no_grad()
def test(
    model: nn.Module,
    loader,
    device
):
    model.eval()
    pred_ls = []
    labels_ls = []
    for (human_pose, pos, shuttle), video_len, labels in loader:
        human_pose: Tensor = human_pose.to(device)
        shuttle: Tensor = shuttle.to(device)
        pos: Tensor = pos.to(device)
        video_len: Tensor = video_len.to(device)

        human_pose = human_pose.view(*human_pose.shape[:-2], -1)
        logits = model(human_pose, shuttle, pos, video_len)

        pred = torch.argmax(logits, dim=1).cpu()
        
        pred_ls.append(pred)
        labels_ls.append(labels)

    return torch.cat(pred_ls), torch.cat(labels_ls)


@torch.no_grad()
def test_topk(
    model: nn.Module,
    loader,
    device,
    k=2
):
    model.eval()
    pred_ls = []
    labels_ls = []
    for (human_pose, pos, shuttle), video_len, labels in loader:
        human_pose: Tensor = human_pose.to(device)
        shuttle: Tensor = shuttle.to(device)
        pos: Tensor = pos.to(device)
        video_len: Tensor = video_len.to(device)

        human_pose = human_pose.view(*human_pose.shape[:-2], -1)
        logits = model(human_pose, shuttle, pos, video_len)

        _, pred = torch.topk(logits, k=k, dim=1)
        
        pred_ls.append(pred.cpu())
        labels_ls.append(labels)

    return torch.cat(pred_ls), torch.cat(labels_ls)


def train_network(
    model: nn.Module,
    train_loader,
    val_loader,
    device,
    save_path: Path,
    n_bones
):
    random_shift_fn = RandomTranslation_batch()

    loss_fn = nn.CrossEntropyLoss(label_smoothing=0.1)
    optimizer = optim.AdamW(model.parameters(), lr=hyp.lr)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=hyp.warm_up_step,
        num_training_steps=(hyp.n_epochs * len(train_loader)),
        num_cycles=0.25
    )

    best_value = 0.0
    early_stop_count = 0

    for epoch in range(1, hyp.n_epochs+1):
        t0 = time.time()
        train_loss = train_one_epoch(
            model=model,
            loader=train_loader,
            random_shift_fn=random_shift_fn,
            n_bones=n_bones,
            loss_fn=loss_fn,
            optimizer=optimizer,
            scheduler=scheduler,
            device=device
        )
        val_loss, f1_score_avg, f1_score_min = validate(
            model=model,
            loss_fn=loss_fn,
            loader=val_loader,
            device=device
        )
        t1 = time.time()
        print(f'Epoch({epoch}/{hyp.n_epochs}): train_loss={train_loss:.3f}, '\
              f'val_loss={val_loss:.3f}, macro_f1={f1_score_avg:.3f}, min_f1={f1_score_min:.3f} '\
              f'- {t1 - t0:.2f} s')

        early_stop_count += 1
        if best_value < f1_score_avg:
            best_value = f1_score_avg
            best_state = deepcopy(model.state_dict())
            print(f'Picked! => Best value {f1_score_avg:.3f}')
            early_stop_count = 0
        
        if early_stop_count == hyp.early_stop_n_epochs:
            print(f'Early stop with best value {best_value:.3f}')
            break

    torch.save(best_state, str(save_path))
    model.load_state_dict(best_state)
    return model


class Task:
    def __init__(self, n_joints=17) -> None:
        self.use_cuda = torch.cuda.is_available()
        self.device = 'cuda' if self.use_cuda else 'cpu'
        self.n_joints = n_joints

    def prepare_dataloaders(
        self,
        root_dir: Path,
        pose_style='Jn2B',
        train_partial=1.0
    ):
        self.train_loader, \
        self.val_loader, \
        self.test_loader \
            = prepare_npy_collated_loaders(
                root_dir=root_dir,
                pose_style=pose_style,
                batch_size=hyp.batch_size,
                use_cuda=self.use_cuda,
                num_workers=(0, 0, 0),
                train_partial=train_partial
            )
        
        self.pose_style = pose_style

    def get_network_architecture(self, model_name, in_channels=2):
        '''
        `model_name`
        - 'BST' (about 1.89M)
        - 'BST_2' (about 1.85M)
        - 'BST_3' (about 1.85M)
        - 'BST_3_2' (about 1.85M)
        - 'BST_4' (about 1.83M)
        - 'BST_5' (about 1.83M)
        - 'BST_5_2' (about 1.86M)
        - 'BST_5_3' (about 1.85M)
        - 'BST_5_4' (about 1.88M)
        - 'BST_5_5' (about 1.84M)
        - 'BST_5_6' (about 1.84M)
        - 'BST_5_7' (about 1.84M)
        - 'BST_5_8' (about 1.82M)
        - 'BST_5_9' (about 1.82M)
        - 'BST_6' (about 1.86M)
        - 'BST_6_2' (about 1.88M)
        - 'BST_7' (about 1.82M)
        - 'BST_7_2' (about 1.78M)
        - 'BST_7_3' (about 1.86M)
        - 'BST_8' (about 1.88M)
        - 'BST_8_2' (about 1.88M)
        '''
        n_bones = len(get_bone_pairs())

        match self.pose_style:
            case 'J_only':
                extra = 0
            case 'JnB_bone' | 'JnB_interp':
                extra = 1
            case 'Jn2B':
                extra = 2

        match model_name:
            case 'BST':
                net = BST(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_2':
                net = BST_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_3':
                net = BST_3(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_3_2':
                net = BST_3_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_4':
                net = BST_4(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5':
                net = BST_5(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_2':
                net = BST_5_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_3':
                net = BST_5_3(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_4':
                net = BST_5_4(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_5':
                net = BST_5_5(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_6':
                net = BST_5_6(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_7':
                net = BST_5_7(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_8':
                net = BST_5_8(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_5_9':
                net = BST_5_9(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_6':
                net = BST_6(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_6_2':
                net = BST_6_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_7':
                net = BST_7(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_7_2':
                net = BST_7_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_7_3':
                net = BST_7_3(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_8':
                net = BST_8(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case 'BST_8_2':
                net = BST_8_2(
                    in_dim=(self.n_joints + n_bones * extra) * in_channels,
                    n_class=hyp.n_classes,
                    seq_len=hyp.seq_len,
                    depth_tem=2,
                    depth_inter=1
                )

            case _:
                raise NotImplementedError
        
        self.model_name = model_name
        self.net = net.to(self.device)
        self.n_bones = n_bones

    def seek_network_weights(self, model_info='', serial_no=1):
        serial_str = f'_{serial_no}' if serial_no != 1 else ''
        model_info = f'_{model_info}' if model_info != '' else ''
        model_postfix = '_' + self.pose_style + model_info + serial_str

        self.model_name = self.model_name.lower() + model_postfix

        weight_path = Path(f'weight/{self.model_name}.pt')
        if weight_path.exists():
            self.net.load_state_dict(torch.load(str(weight_path), map_location=self.device, weights_only=True))
        else:
            train_t0 = time.time()
            self.net = train_network(
                model=self.net,
                train_loader=self.train_loader,
                val_loader=self.val_loader,
                device=self.device,
                save_path=weight_path,
                n_bones=len(get_bone_pairs()) if self.pose_style != 'J_only' else 0
            )
            train_t1 = time.time()
            t = timedelta(seconds=int(train_t1 - train_t0))
            print(f'Total training time: {t}')

    def test(self, show_details=False, show_confusion_matrix=False):
        pred, gt = test(self.net, self.test_loader, self.device)
        print(f'Test (num_strokes: {len(pred)}) =>')

        f1_score_each = multiclass_f1_score(pred, gt, num_classes=hyp.n_classes, average=None)
        show_f1_results(
            model_name=self.model_name,
            f1_score_each=f1_score_each,
            show_details=show_details
        )

        acc = torch.sum(pred == gt).item() / len(pred)
        print('Accuracy:', f'{acc:.3f}')

        if show_confusion_matrix:
            plot_confusion_matrix(
                y_true=gt,
                y_pred=pred,
                need_pre_argmax=False,
                model_name=self.model_name,
                font_size=6,
                save=False
            )

    def test_topk_acc(self, k=2):
        assert k > 1, 'k should be > 1'
        pred, gt = test_topk(self.net, self.test_loader, self.device, k=k)
        gt = gt.unsqueeze(1).repeat(1, k)
        acc = torch.any(pred == gt, dim=1).sum().item() / len(gt)
        print(f'Top{k} Accuracy: {acc:.3f}')

    def compare_pred_gt_on_specific_type(self, dir_path: Path):
        infer_ds = Dataset_npy(
            root_dir=dir_path,
            set_name='test_specific',
            pose_style=self.pose_style,
            seq_len=hyp.seq_len
        )
        infer_loader = DataLoader(
            dataset=infer_ds,
            batch_size=hyp.batch_size,
        )

        pred, gt = test(self.net, infer_loader, self.device)
        pred = pred.cpu().numpy()
        gt = gt.cpu().numpy()

        with pd.option_context('display.max_rows', None):
            df = pd.DataFrame(
                data={
                    'Ball Round': [Path(e).stem for e in infer_ds.data_branches],
                    'Pred': pred,
                    'GT': gt
                }
            )
            print(df)


if __name__ == '__main__':
    task = Task(n_joints=17)
    task.prepare_dataloaders(
        root_dir=Path('dataset_npy_collated_between_2_hits_with_max_limits_seq_100'),
        pose_style='JnB_bone',
        train_partial=1
    )
    task.get_network_architecture(model_name='BST_8', in_channels=2)
    task.seek_network_weights(model_info='between_2_hits_with_max_limits_seq_100', serial_no=11)
    task.test(show_details=False, show_confusion_matrix=False)
    task.test_topk_acc(k=2)
    # task.compare_pred_gt_on_specific_type(
    #     Path("C:/MyResearch/stroke_classification/dataset_npy/test/Top_點扣")
    # )
