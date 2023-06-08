from header import *
from dataloader import *
from models import *
from config import *
import sys

def parser_args():
    parser = argparse.ArgumentParser(description='train parameters')
    parser.add_argument('--dataset', default='ecommerce', type=str)
    parser.add_argument('--model', type=str)
    parser.add_argument('--mode', type=str, default='train')
    parser.add_argument('--loss_type', type=str, default='focal_loss')
    parser.add_argument('--version', type=str)
    parser.add_argument('--pretrain_model_path', type=str)
    parser.add_argument('--training_data_dir', type=str)
    parser.add_argument('--multi_gpu', type=str, default=None)
    parser.add_argument('--local_rank', type=int)
    parser.add_argument('--prebatch_phrase_num', type=int)
    parser.add_argument('--total_workers', type=int)
    parser.add_argument('--resume', type=bool, default=False)
    parser.add_argument('--random_initialize', type=bool, default=False)
    parser.add_argument('--normalize', type=bool, default=False)
    parser.add_argument('--in_doc_neg_num', type=int, default=0)
    parser.add_argument('--data_file_num', type=int, default=8)
    parser.add_argument('--temp', type=float, default=1.0)
    parser.add_argument('--beta', type=float, default=0.7)
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--iter_to_accumulate', type=int, default=1)
    parser.add_argument('--warmup_step', type=int, default=0)
    return parser.parse_args()


def main(**args):
    torch.cuda.empty_cache()
    torch.cuda.set_device(args['local_rank'])
    torch.distributed.init_process_group(backend='nccl', init_method='env://')
    args['global_rank'] = dist.get_rank()
    print(f'[!] global rank: {args["global_rank"]}')

    config = load_config(args)
    args.update(config)

    __stderr__ = sys.stderr
    sys.stderr = open(f'{args["log_dir"]}/{args["mode"]}/{args["version"]}.error', 'a')

    train_data, train_iter, sampler = load_dataset(args)
    
    if args['local_rank'] == 0:
        # sum_writer = SummaryWriter(
        #     log_dir=f'{args["root_dir"]}/rest/{args["dataset"]}/{args["model"]}/{args["mode"]}/{args["version"]}',
        # )
        sum_writer = None
        print('log dir: ', args['log_dir'])
    else:
        sum_writer = None
        
    agent = load_model(args)
    # pbar = tqdm(total=args['total_step'])
    pbar = None
    current_step, over_train_flag = 0, False
    # sampler.set_epoch(0)    # shuffle for DDP
    if agent.load_last_step:
        current_step = agent.load_last_step + 1
        if args['global_rank'] == 0:
            print(f'[!] load latest step: {current_step}')
    while True:
        for batch in train_iter:
            current_step += 1
            agent.train_model(
                batch, 
                recoder=sum_writer, 
                current_step=current_step, 
                pbar=pbar
            )
            if args['global_rank'] == 0 and current_step % args['save_every'] == 0:
                save_path = f'{args["root_dir"]}/ckpt/{args["dataset"]}/{args["model"]}/{args["mode"]}/best_{args["version"]}_{current_step}.pt'
                agent.save_model_long(save_path, current_step)
            
            if current_step >= args['total_step']:
                over_train_flag = True
                break
        if over_train_flag:
            break
        # if args['global_rank'] == 0:
        #     save_path = f'{args["root_dir"]}/ckpt/{args["dataset"]}/{args["model"]}/{args["mode"]}/best_{args["version"]}_epoch{epoch}.pt'
        #     agent.save_model_long(save_path, current_step)
    if sum_writer:
        sum_writer.close()

if __name__ == "__main__":
    args = parser_args()
    args = vars(args)
    main(**args)
