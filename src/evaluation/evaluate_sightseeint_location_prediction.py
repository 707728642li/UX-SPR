import argparse
import csv
import os
from os.path import join, dirname
import pandas as pd
import numpy as np
import time
from dotenv import load_dotenv
from collections import defaultdict

import boto3
import comet_ml
import pyro
import pyro.distributions as dist
import torch
from botocore.exceptions import ClientError
from comet_ml import api
from tqdm import tqdm

device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")

load_dotenv(verbose=True)
dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)


def download_posterior(exp_key):
    session = boto3.Session(profile_name=os.environ.get('AWS_PROFILE'))
    s3 = session.client('s3')

    bucket_name = os.environ.get('AWS_BUCKET')
    file_name = exp_key + '.pkl'
    try:
        s3.download_file(bucket_name, file_name, file_name)
    except ClientError as e:
        print(e)
        raise


def get_test_data(data_file, test_ids):
    with open(data_file) as f:
        reader = csv.reader(f)

        us = []
        ts = []
        ls = []
        tag_matrix = []

        for i, row in enumerate(reader):
            #if i in test_ids:
                us.append(int(row[1]))
                ts.append(int(row[2]))
                ls.append(int(row[3]))
                tag_matrix.append([int(t) for t in row[4].split(",")])

    data = {
        'u': torch.LongTensor(us).to(device),
        't': torch.LongTensor(ts).to(device),
        'l': torch.LongTensor(ls).to(device),
        'tag': torch.LongTensor(tag_matrix).to(device)
    }

    return data


def get_training_data(data_file, test_ids):
    with open(data_file) as f:
        reader = csv.reader(f)

        us = []
        ts = []
        ls = []
        tag_matrix = []

        for i, row in enumerate(reader):
            #if i not in test_ids:
                us.append(int(row[1]))
                ts.append(int(row[2]))
                ls.append(int(row[3]))
                tag_matrix.append([int(t) for t in row[4].split(",")])

    data = {
        'u': torch.LongTensor(us).to(device),
        't': torch.LongTensor(ts).to(device),
        'l': torch.LongTensor(ls).to(device),
        'tag': torch.LongTensor(tag_matrix).to(device)
    }

    return data


def divide_data_by_user(data, posterior, method='loc'):
    user_count = max(posterior['gamma_q'].shape[1],data['u'].max().item()+1)
    location_count = max(posterior['beta_q'].shape[1],data['l'].max().item()+1)
    activity_count = max(posterior['delta_q'].shape[1],data['tag'].max().item()+1)

    user_location_matrix = torch.zeros(user_count, location_count).to(device)

    for u, l in zip(data['u'], data['l']):
        user_location_matrix[u][l] += 1

    user_activity_matrix = torch.zeros(user_count, activity_count).to(device)

    for u, l in zip(data['u'], data['tag']):
        user_activity_matrix[u][l] += 1
    if method == 'loc':
      return user_location_matrix
    else:
      return user_activity_matrix


def calculate_scores_for_images(images_prob, n_length, distribution_method):
    # n_length : the number of replication
    # calculate ranking probability, the distribution of ranking for each image
    if distribution_method not in ['uniform', 'normal']:
        return

    a_length = len(images_prob[0, :])
    ranking = torch.zeros(a_length, 100, 10, device=device)
    for j in range(0, 10):
        temp_prob = images_prob[j, :].numpy()
        temp_prob /= sum(temp_prob)
        for i in range(0, n_length):
            temp = torch.tensor(np.random.choice(range(0, a_length), a_length, replace=False, p=temp_prob),
                                device=device)
            for ii in range(1, 101):
                ranking[temp[round(a_length / 100 * (ii - 1)):round(a_length / 100 * ii)], ii - 1, j] += 1
    # ranking : (image_id, image_scores)

    ranking = ranking / n_length
    ranking_final = torch.zeros(a_length, 5, 10, device=device)
    if distribution_method == "uniform":
        ranking_final[:, 4, :] = ranking[:, 0:20, :].sum(1)
        ranking_final[:, 3, :] = ranking[:, 20:40, :].sum(1)
        ranking_final[:, 2, :] = ranking[:, 40:60, :].sum(1)
        ranking_final[:, 1, :] = ranking[:, 60:80, :].sum(1)
        ranking_final[:, 0, :] = ranking[:, 80:100, :].sum(1)

    if distribution_method == "normal":
        ranking_final[:, 4, :] = ranking[:, 0:7, :].sum(1)
        ranking_final[:, 3, :] = ranking[:, 7:31, :].sum(1)
        ranking_final[:, 2, :] = ranking[:, 31:69, :].sum(1)
        ranking_final[:, 1, :] = ranking[:, 69:93, :].sum(1)
        ranking_final[:, 0, :] = ranking[:, 93:100, :].sum(1)

    return ranking_final


def calculate_weights_using_scores(ranking, scores, weights_old):
    # scores_from_user : (image_id, scores), weights_old : weights of 10 groups
    # calculate weights of each group using the ranking probability, which is calculated from scores rated by user
    if sum(weights_old) != 1:
      weights_old /= sum(weights_old)
    weights = []
    for i in range(0, 10):
        weights.append(weights_old[i] * (ranking[scores[0, :], scores[1, :], i]+0.00001).prod())

    return torch.tensor(weights)/sum(weights)


def calculate_weights_using_scores_cf(loc_prob, scores, weights_old, method='NA'):
    weights = []
    if method == "regular" :
        for i in range(0, 10):
            weights.append(weights_old[i] * sum(loc_prob[i, scores[0, :]]*(scores[1, :]-2))
                /torch.sqrt((loc_prob[i, scores[0, :]]*loc_prob[i, scores[0, :]]).sum()*(scores[1, :]*scores[1, :]).sum()))
    elif method == 'prod':
        for i in range(0, 10):
            weights.append(weights_old[i] * loc_prob[i, scores[0, 0]] * (1-loc_prob[i, scores[0, 1:]]).prod()
                      /torch.sqrt((loc_prob[i, scores[0, :]]*loc_prob[i, scores[0, :]]).sum()*(scores[1, :]*scores[1, :]).sum())*weights_old[i])
    elif method == 'exp':
        for i in range(0, 10):
            weights.append( weights_old[i] * torch.exp(sum(loc_prob[i, scores[0, :]]*scores[1, :])))
    else:
        for i in range(0, 10):
            weights.append( loc_prob[i, scores[0, 0]] )#* (1-loc_prob[i, scores[0, 1:]]).prod())
            #weights.append(weights_old[i] * (loc_prob[i, scores[0, 0]]^scores[1, 0]).prod())
            #weights.append(weights_old[i] * loc_prob[i, scores[0, 0]] * (1-loc_prob[i, scores[0, 1:]]).prod())
            #weights.append( weights_old[i] * torch.exp(sum(loc_prob[i, scores[0, :]]*scores[1, :])))
            #/torch.sqrt((loc_prob[i, scores[0, :]]*loc_prob[i, scores[0, :]]).sum()*(scores[1, :]*scores[1, :]).sum())*weights_old[i] )

    return torch.tensor(weights)/sum(weights)


def recommend_according_to_weights(weights, est_prob, used_for_scores):
    # weights.size : 1*10; est_prob.size : 10*length
    # recommend image/location/activity according to weights and estimated probability
    rec_prob = (weights.unsqueeze(1) * est_prob).sum(0)
    recommend = torch.argsort(rec_prob, descending=True)
    if used_for_scores == "NA":
        return recommend
    else :
        for ijk in range(0, len(used_for_scores)):
            recommend = torch.cat((recommend[recommend != used_for_scores[ijk]], recommend[recommend == used_for_scores[ijk]]),dim=0)
            return recommend


def let_users_give_scores(data):
    high_score_place = list(np.random.choice((data != 0).nonzero().squeeze(), 1, replace=False))
    if (data == 0).sum()<4 :
      low_score_place = list(np.random.choice((data == 0).nonzero().squeeze(), (data == 0).sum().item(), replace=False))
    else:
      low_score_place = list(np.random.choice((data == 0).nonzero().squeeze(), 4, replace=False))
    high_score_place.extend(low_score_place)
    high_score = list(np.random.choice([3,4], 1, p=[0.7742, 0.2258]))
    low_score = list(np.random.choice([2,1,0], len(low_score_place), replace=True, p=[0.5508, 0.3478, 0.1014]))
    #high_score = [0]
    #low_score = [1, 1, 1, 1]
    high_score.extend(low_score)
    scores = [high_score_place, high_score]
    scores = torch.tensor(scores)

    return scores

def let_users_give_scores_likedislike(data):
    high_score_place = list(np.random.choice((data != 0).nonzero().squeeze(), 1, replace=False))
    low_score_place = list(np.random.choice((data == 0).nonzero().squeeze(), 4, replace=False))
    high_score_place.extend(low_score_place)
    high_score = list(np.random.choice(range(3, 4), 1))
    low_score = list(np.random.choice(range(1, 2), 4, replace=True))
    high_score.extend(low_score)
    scores = [high_score_place, high_score]
    scores = torch.tensor(scores)

    return scores


def scores_from_training_set(data):
    if (data != 0).nonzero().size(0) > 1 :
      high_score_place = list(np.random.choice((data != 0).nonzero().squeeze(), (data != 0).nonzero().size(0), replace=False))
      high_score = list(np.random.choice([3,4], len(high_score_place), p=[0.7742, 0.2258], replace=True))
      scores = [high_score_place, high_score]
      scores = torch.tensor(scores)
    else:
      high_score_place = list(np.random.choice([(data != 0).nonzero().squeeze().item()], (data != 0).nonzero().size(0), replace=False))
      high_score = list(np.random.choice([3,4], len(high_score_place), p=[0.7742, 0.2258], replace=True))
      scores = [high_score_place, high_score]
      scores = torch.tensor(scores)

    return scores

def likedislike_from_training_set(data):
    if (data != 0).nonzero().size(0) > 1 :
      high_score_place = list(np.random.choice((data != 0).nonzero().squeeze(), (data != 0).nonzero().size(0), replace=False))
      high_score = list(np.random.choice(range(3,4), len(high_score_place), replace=True))
      scores = [high_score_place, high_score]
      scores = torch.tensor(scores)
    else:
      high_score_place = list(np.random.choice([(data != 0).nonzero().squeeze().item()], (data != 0).nonzero().size(0), replace=False))
      high_score = list(np.random.choice(range(3,4), len(high_score_place), replace=True))
      scores = [high_score_place, high_score]
      scores = torch.tensor(scores)

    return scores


def let_user_give_feedback(data, recommend, k):
    scores = []
    recommend_test = recommend.tolist()
    for ijk in range(0, k):
        if (data[recommend[ijk]] != 0):
            scores.extend(list(np.random.choice(range(3, 5), 1)))
        else:
            scores.extend(list(np.random.choice(range(0, 3), 1)))
    scores_temp = [recommend_test, scores]
    feedback_info = torch.tensor(scores_temp)

    return feedback_info


def create_location_ranking(posterior, sample_size, method='NA'):
    alpha_q = posterior['alpha_q'].to(device)
    gamma_q = posterior['gamma_q'].to(device)
    beta_q = posterior['beta_q'].to(device)

    size = torch.LongTensor([sample_size]).to(device)
    
    theta = dist.Dirichlet(alpha_q).sample(size)
    pi = dist.Dirichlet(gamma_q).sample(size)
    phi = dist.Dirichlet(beta_q).sample(size)
    pi_star = (pi.mean()*torch.ones(pi.unsqueeze(3).size()))

    if method == 'mix':
        probs = (theta.view(size, -1, 1, 1) * pi.unsqueeze(3) * phi.unsqueeze(2) + 
             theta.view(size, -1, 1, 1) * phi.unsqueeze(2) * pi_star).sum(1)
    else:
        probs = (theta.view(size, -1, 1, 1) * pi.unsqueeze(3) * phi.unsqueeze(2)).sum(1)
    #probs = (theta.view(size, -1, 1, 1) * pi_star.unsqueeze(3) * phi.unsqueeze(2)).sum(1).mean(0).unsqueeze(3)
    ranking = torch.argsort(probs, dim=2, descending=True)
    # ranking: (sample_size, user_count, location_count)

    return ranking


def evaluation_pre_and_recall(recommend_rank, k, data, save_path='NA'):
    
    recommend_top_k = recommend_rank[:, :, :k].to(torch.int64)
    
    
    recommend_top_k_bag = torch.zeros(recommend_rank.shape).to(device).scatter_(2, recommend_top_k, 1)
    
    top_k_correct_bag = torch.logical_and(recommend_top_k_bag, data.expand(recommend_top_k_bag.shape))
    top_k_correct_count_per_user = torch.mean(top_k_correct_bag.type(torch.Tensor).sum(2), 0).to(device)
    top_k_correct_count_per_location = torch.mean(top_k_correct_bag.type(torch.Tensor).sum(1), 0).to(device)
    l_rec_number = torch.ones(recommend_rank.size(2))
    for i_rec in range(recommend_rank.size(2)):
      if (recommend_top_k == i_rec).sum()>0 :
        l_rec_number[i_rec] = (recommend_top_k == i_rec).sum()/recommend_rank.size(0)
    user_count_having_locations = torch.sum(data.sum(1) > 0).to(device)

    #20210908 User-Oriented Fairness
    advan_n = torch.round(user_count_having_locations*0.1)
    data_advan = torch.argsort((data != 0).sum(1),descending=True)[:(advan_n.int())]
    data_disadvan = torch.argsort((data != 0).sum(1),descending=True)[(advan_n.int()):]

    precision_ad = torch.sum(top_k_correct_count_per_user[data_advan]) / (advan_n.int()) / k
    recalls_ad = top_k_correct_count_per_user[data_advan] / (data[data_advan, :]!=0).sum(1)
    recalls_ad[torch.isnan(recalls_ad)] = 0
    recalls_ad[recalls_ad == float('inf')] = 0
    recall_ad = torch.sum(recalls_ad) / (advan_n.int())
    F_advan = 2/(1/precision_ad+1/recall_ad)

    precision_dis = torch.sum(top_k_correct_count_per_user[data_disadvan]) / (user_count_having_locations-(advan_n.int())) / k
    recalls_dis = top_k_correct_count_per_user[data_disadvan] / (data[data_disadvan, :]!=0).sum(1)
    recalls_dis[torch.isnan(recalls_dis)] = 0
    recalls_dis[recalls_dis == float('inf')] = 0
    recall_dis = torch.sum(recalls_dis) / (user_count_having_locations-(advan_n.int()))
    F_disadvan = 2/(1/precision_dis+1/recall_dis)

    UOF = F_advan-F_disadvan

    precision = torch.sum(top_k_correct_count_per_user) / user_count_having_locations / k
    recalls = top_k_correct_count_per_user / (data!=0).sum(1)
    recalls[torch.isnan(recalls)] = 0
    recalls[recalls == float('inf')] = 0
    recall = torch.sum(recalls) / user_count_having_locations

    recall_loc = top_k_correct_count_per_location / (data!=0).sum(0)
    recall_loc[torch.isnan(recall_loc)] = 0
    recall_loc[recall_loc == float('inf')] = 0

    #20210814new fairness
    #user_fairness = torch.absolute(top_k_correct_count_per_user-torch.mean(top_k_correct_count_per_user)).sum()
    pre_per_user = top_k_correct_count_per_user/k
    recall_per_user = recalls
    pre_per_loc = top_k_correct_count_per_location/l_rec_number
    recall_per_loc = recall_loc
    user_fairness = torch.std((top_k_correct_count_per_user/k))
    location_fairness = torch.std(pre_per_loc)

    #20210819 F-measure
    F_measure = 2/(1/precision+1/recall)

    pre_recall_at_k = {f"precision@{k}": precision.to('cpu').item(),
                       f"recall@{k}": recall.to('cpu').item(),
                       f"u_fairness@{k}": user_fairness.to('cpu').item(),
                       f"l_fairness@{k}": location_fairness.to('cpu').item(),
                       f"F_measure@{k}": F_measure.to('cpu').item(),
                       f"UOF@{k}": UOF.to('cpu').item()}
    # save 2021/09/19
    if save_path!='NA':
      torch.save(pre_per_user,save_path+'_'+str(k)+'_user_precision.pkl')
      torch.save(pre_per_loc,save_path+'_'+str(k)+'_loc_precision.pkl')
      torch.save(recall_per_user,save_path+'_'+str(k)+'_user_recall.pkl')
      torch.save(recall_per_loc,save_path+'_'+str(k)+'_loc_recall.pkl')
    return pre_recall_at_k


def calc_score_base(posterior, data, method='NA',save_path='NA'):

    result_metrics = {}
    sample_size = 10
    ranking = create_location_ranking(posterior, sample_size, method)

    pre_recall_at_one = evaluate_location_precision_and_recall(ranking, 1, data,save_path)
    result_metrics.update(pre_recall_at_one)
    print(pre_recall_at_one)

    pre_recall_at_five = evaluate_location_precision_and_recall(ranking, 5, data,save_path)
    result_metrics.update(pre_recall_at_five)
    print(pre_recall_at_five)

    pre_recall_at_ten = evaluate_location_precision_and_recall(ranking, 10, data,save_path)
    result_metrics.update(pre_recall_at_ten)
    print(pre_recall_at_ten)

    pre_recall_at_fifteen = evaluate_location_precision_and_recall(ranking, 15, data,save_path)
    result_metrics.update(pre_recall_at_fifteen)
    print(pre_recall_at_fifteen)

    pre_recall_at_twenty = evaluate_location_precision_and_recall(ranking, 20, data,save_path)
    result_metrics.update(pre_recall_at_twenty)
    print(pre_recall_at_twenty)

    return result_metrics


def calc_score(recommend, data_per_user, save_path='NA'):

    result_metrics = {}
    pre_recall_at_one = evaluation_pre_and_recall(recommend, 1, data_per_user, save_path)
    result_metrics.update(pre_recall_at_one)
    print(pre_recall_at_one)

    pre_recall_at_five = evaluation_pre_and_recall(recommend, 5, data_per_user, save_path)
    result_metrics.update(pre_recall_at_five)
    print(pre_recall_at_five)

    pre_recall_at_ten = evaluation_pre_and_recall(recommend, 10, data_per_user, save_path)
    result_metrics.update(pre_recall_at_ten)
    print(pre_recall_at_ten)

    pre_recall_at_fifteen = evaluation_pre_and_recall(recommend, 15, data_per_user, save_path)
    result_metrics.update(pre_recall_at_fifteen)
    print(pre_recall_at_fifteen)

    pre_recall_at_twenty = evaluation_pre_and_recall(recommend, 20, data_per_user, save_path)
    result_metrics.update(pre_recall_at_twenty)
    print(pre_recall_at_twenty)

    return result_metrics



def run(ex):
    eid = ex.id
    # filter
    if not ex.get_metrics(metric="duration"):
        # not yet finished
        print('Not finished: {} \n'.format(eid))
        return

    try:
        print('start: ', eid)

        # download posterior
        download_posterior(eid)

        posterior = torch.load(eid + '.pkl')
      
        repeat_time = 10
        test_data = get_test_data(posterior['data_file'], posterior['test_ids'])
        train_data = get_training_data(posterior['data_file'], posterior['test_ids'])

        alpha_q = posterior['alpha_q']
        beta_q = posterior['beta_q']
        delta_q = posterior['delta_q']
        theta = dist.Dirichlet(alpha_q).sample(torch.LongTensor([10000]))
        phi = dist.Dirichlet(beta_q).sample(torch.LongTensor([10000]))
        sigma = dist.Dirichlet(delta_q).sample(torch.LongTensor([10000]))
        locs_prob = phi.mean(0)
        acts_prob = sigma.mean(0)

        loc_ranking_temp = calculate_scores_for_images(locs_prob, 1000, "normal")
        act_ranking_temp = calculate_scores_for_images(acts_prob, 1000, "normal")

        loc_per_user_new = test_loc_per_user[((test_loc_per_user != 0).sum(1) > 0), :locs_prob.size(1)]
        act_per_user_new = test_act_per_user[((test_act_per_user != 0).sum(1) > 0), :acts_prob.size(1)]

        loc_train_per_user = divide_data_by_user(get_training_data(train_data_path, posterior['test_ids']), posterior, method='loc')
        act_train_per_user = divide_data_by_user(get_training_data(train_data_path, posterior['test_ids']), posterior, method='act')

        data_per_user_new = loc_per_user_new

        recommend_al = torch.zeros(repeat_time, data_per_user_new.size(0), locs_prob.size(1))
        recommend_wtol = torch.zeros(repeat_time, data_per_user_new.size(0), locs_prob.size(1))
        recommend_wl = torch.zeros(repeat_time, data_per_user_new.size(0), locs_prob.size(1))

        for i_r in range(repeat_time):
          weights_ini = theta[i_r, :]
          for iii in range(0, data_per_user_new.size(0)):

            if data_type == 'time' :
              if ((loc_train_per_user[iii, :]!=0).sum()>0)&((act_train_per_user[iii, :]!=0).sum()>0):
                loc_scores_from_user = scores_from_training_set(loc_train_per_user[iii, :])
                act_scores_from_user = scores_from_training_set(act_train_per_user[iii, :])
              else:
                loc_scores_from_user = torch.tensor([[0,1],[1,1]])
                act_scores_from_user = torch.tensor([[0,1],[1,1]])
            else:
              if ((loc_per_user_new[iii, :]!=0).sum()>0)&((act_per_user_new[iii, :]!=0).sum()>0):
                loc_scores_from_user = let_users_give_scores(loc_per_user_new[iii, :])
                act_scores_from_user = let_users_give_scores(act_per_user_new[iii, :])
              else:
                loc_scores_from_user = torch.tensor([[0,1],[1,1]])
                act_scores_from_user = torch.tensor([[0,1],[1,1]])

            weights_al = calculate_weights_using_scores(loc_ranking_temp, loc_scores_from_user, weights_ini)
            recommend_al[i_r, iii, :] = recommend_according_to_weights(weights_al, locs_prob, 'NA')#loc_scores_from_user[0, :])

            weights_wtol = calculate_weights_using_scores(act_ranking_temp, act_scores_from_user, weights_ini)
            recommend_wtol[i_r, iii, :] = recommend_according_to_weights(weights_wtol, locs_prob, 'NA')#loc_scores_from_user[0, :])

            weights_wl = calculate_weights_using_scores(loc_ranking_temp, loc_scores_from_user, weights_ini)
            weights_wl = calculate_weights_using_scores(act_ranking_temp, act_scores_from_user, weights_wl)
            recommend_wl[i_r, iii, :] = recommend_according_to_weights(weights_wl, locs_prob, 'NA')#loc_scores_from_user[0, :])

        metrics_base = calc_score_base(posterior, data_per_user)
        metrics_al = calc_score(recommend_al, data_per_user_new)
        metrics_wl = calc_score(recommend_wl, data_per_user_new)
        metrics_wtol = calc_score(recommend_wtol, data_per_user_new)

        # rm pickl
        if os.path.exists(eid + '.pkl'):
            os.remove(eid + '.pkl')
        else:
            print('Could not remove: ', eid + '.pkl')

        print('done: ', eid, "\n")
    except ClientError:
        print('error: ', eid, "\n")
        return


def main(args):
    print(args)
    api_key = os.environ.get('COMET_API_KEY')
    workspace_name = os.environ.get('COMET_WORKSPACE')
    if args.debug:
        project_name = 'test'
    else:
        project_name = os.environ.get('COMET_PROJECT')

    # get experiments
    api_instance = api.API(api_key=api_key)
    q = ((api.Metric('duration') != None) & (api.Parameter('group_count') <= 15))
    exs = api_instance.query(workspace_name, project_name, q)

    for ex in exs:
        run(ex)


if __name__ == '__main__':
    assert pyro.__version__.startswith('1.3.1')
    pyro.enable_validation()
    parser = argparse.ArgumentParser(description='pyro model evaluation')
    parser.add_argument('--debug', action='store_true', help='debug mode')

    args = parser.parse_args()

    main(args)
