import os
import random
import networkx as nx
import copy
import logging
import pickle
import pandas as pd
import community as community_louvain

import matplotlib.pyplot as plt
import seaborn as sns

import torch
from torch_geometric.datasets import CitationFull
from torch_geometric.data import Data, DataLoader
from torch_geometric.utils import k_hop_subgraph, from_networkx

from FedML.fedml_core.non_iid_partition.noniid_partition import partition_class_samples_with_dirichlet_distribution


def _subgraphing(g, partion):
    nodelist = [None] * len(set(partion.values()))
    for k, v in partion.items():
        if nodelist[v] is None:
            nodelist[v] = []
        nodelist[v].append(k)

    graphs = []
    for nodes in nodelist:
        if len(nodes) < 2:
            continue
        graphs.append(from_networkx(nx.subgraph(g, nodes)))
    return graphs


def _read_mapping(path, data, filename):
    mapping = dict()
    df = pd.read_csv(os.path.join(path, data, filename), sep='\t', header=None, index_col=None)
    for _, row in df.iterrows():
        mapping[row[1]] = int(row[0])
    # with open(os.path.join(path, data, filename)) as f:
    #     for line in f:
    #         s = line.strip().split()
    #         mapping[s[1]] = int(s[0])
    
    return mapping


def _build_nxGraph(path, data, filename, mapping_entities, mapping_relations):
    G = nx.Graph()
    df = pd.read_csv(os.path.join(path, data, filename), sep='\t', header=None, index_col=None)
    for _, row in df.iterrows():
        G.add_edge(mapping_entities[row[0]], mapping_entities[row[2]], edge_label=mapping_relations[row[1]])
    # with open(os.path.join(path, data, filename)) as f:
    #     for line in f:
    #         s = line.strip().split()
    #         G.add_edge(mapping_entities[s[0]], mapping_entities[s[2]], edge_label=mapping_relations[s[1]])
    return G


def get_data_community(path, data, algo):
    """ For relation type prediction. """

    mapping_entities = _read_mapping(path, data, 'entities.dict')
    mapping_relations = _read_mapping(path, data, 'relations.dict')

    g_train = _build_nxGraph(path, data, 'train.txt', mapping_entities, mapping_relations)
    g_val = _build_nxGraph(path, data, 'valid.txt', mapping_entities, mapping_relations)
    g_test = _build_nxGraph(path, data, 'test.txt', mapping_entities, mapping_relations)

    assert algo in ['Louvain', 'girvan_newman', 'Clauset-Newman-Moore', 'asyn_lpa_communities', 'label_propagation_communities']

    if algo == 'Louvain':
        partion = community_louvain.best_partition(g_train)
        graphs_train = _subgraphing(g_train, partion)
        partion = community_louvain.best_partition(g_val)
        graphs_val = _subgraphing(g_val, partion)
        partion = community_louvain.best_partition(g_test)
        graphs_test = _subgraphing(g_test, partion)

    # algorithms:
    # Louvain
    # girvan_newman
    # greedy_modularity_communities
    # asyn_lpa_communities
    # label_propagation_communities

    return graphs_train, graphs_val, graphs_test


def _build_pygGraph(relType, df, mapping_entities, mapping_relations):
    df[0].replace(mapping_entities, inplace=True)
    df[1].replace(mapping_relations, inplace=True)
    df[2].replace(mapping_entities, inplace=True)

    g = nx.Graph()
    g.add_edges_from(zip(df[0], df[2]), edge_label=mapping_relations[relType])
    
    return from_networkx(g)


def _build_graphs_by_relType(path, data, filename, mapping_entities, mapping_relations):
    df = pd.read_csv(os.path.join(path, data, filename), sep='\t', header=None, index_col=None)
    
    graphs = [_build_pygGraph(relType, group, mapping_entities, mapping_relations) for relType, group in df.groupby(1)]
    return graphs


def get_data_community_byRelType(path, data):
    """ For link prediction with communities grouped by the relation type. """

    mapping_entities = _read_mapping(path, data, 'entities.dict')
    mapping_relations = _read_mapping(path, data, 'relations.dict')

    graphs_train = _build_graphs_by_relType(path, data, 'train.txt', mapping_entities, mapping_relations)
    graphs_val = _build_graphs_by_relType(path, data, 'valid.txt', mapping_entities, mapping_relations)
    graphs_test = _build_graphs_by_relType(path, data, 'test.txt', mapping_entities, mapping_relations)

    # number of graphs == number of relation type
    return graphs_train, graphs_val, graphs_test


def create_random_split(path, data, pred_task='link_prediction', algo='Louvain'):
    assert pred_task in ['relType_prediction', 'link_prediction']

    if pred_task == 'relType_prediction':
        graphs_train, graphs_val, graphs_test = get_data_community(path, data, algo)
    if pred_task == 'link_prediction':
        graphs_train, graphs_val, graphs_test = get_data_community_byRelType(path, data)

    return graphs_train, graphs_val, graphs_test


def create_non_uniform_split(args, idxs, client_number, is_train=True):
    logging.info("create_non_uniform_split------------------------------------------")
    N = len(idxs)
    alpha = args.partition_alpha
    logging.info("sample number = %d, client_number = %d" % (N, client_number))
    logging.info(idxs)
    idx_batch_per_client = [[] for _ in range(client_number)]
    idx_batch_per_client, min_size = partition_class_samples_with_dirichlet_distribution(N, alpha, client_number,
                                                                                         idx_batch_per_client, idxs)
    logging.info(idx_batch_per_client)
    sample_num_distribution = []

    for client_id in range(client_number):
        sample_num_distribution.append(len(idx_batch_per_client[client_id]))
        logging.info("client_id = %d, sample_number = %d" % (client_id, len(idx_batch_per_client[client_id])))
    logging.info("create_non_uniform_split******************************************")

    # plot the (#client, #sample) distribution
    if is_train:
        logging.info(sample_num_distribution)
        plt.hist(sample_num_distribution)
        plt.title("Sample Number Distribution")
        plt.xlabel('number of samples')
        plt.ylabel("number of clients")
        fig_name = "x_hist.png"
        fig_dir = os.path.join("./visualization", fig_name)
        plt.savefig(fig_dir)
    return idx_batch_per_client


def partition_data_by_sample_size(args, path, client_number, uniform=True, compact=True):
    graphs_train, graphs_val, graphs_test = create_random_split(path, args.dataset, args.pred_task, args.part_algo)

    num_train_samples = len(graphs_train)
    num_val_samples = len(graphs_val)
    num_test_samples = len(graphs_test)

    train_idxs = list(range(num_train_samples))
    val_idxs = list(range(num_val_samples))
    test_idxs = list(range(num_test_samples))

    random.shuffle(train_idxs)
    random.shuffle(val_idxs)
    random.shuffle(test_idxs)

    partition_dicts = [None] * client_number

    if uniform:
        clients_idxs_train = np.array_split(train_idxs, client_number)
        clients_idxs_val = np.array_split(val_idxs, client_number)
        clients_idxs_test = np.array_split(test_idxs, client_number)
    else:
        clients_idxs_train = create_non_uniform_split(args, train_idxs, client_number, True)
        clients_idxs_val = create_non_uniform_split(args, val_idxs, client_number, False)
        clients_idxs_test = create_non_uniform_split(args, test_idxs, client_number, False)

    labels_of_all_clients = []
    for client in range(client_number):
        client_train_idxs = clients_idxs_train[client]
        client_val_idxs = clients_idxs_val[client]
        client_test_idxs = clients_idxs_test[client]

        train_graphs_client = [graphs_train[idx] for idx in client_train_idxs]
        train_labels_client = [graphs_train[idx].y for idx in client_train_idxs]
        labels_of_all_clients.append(train_labels_client)

        val_graphs_client = [graphs_val[idx] for idx in client_val_idxs]
        
        val_labels_client = [graphs_val[idx].y for idx in client_val_idxs]
        labels_of_all_clients.append(val_labels_client)

        test_graphs_client = [graphs_test[idx] for idx in client_test_idxs]

        test_labels_client = [graphs_test[idx].y for idx in client_test_idxs]
        labels_of_all_clients.append(test_labels_client)


        partition_dict = {'train': train_graphs_client,
                          'val': val_graphs_client,
                          'test': test_graphs_client}

        partition_dicts[client] = partition_dict

    # plot the label distribution similarity score
    visualize_label_distribution_similarity_score(labels_of_all_clients)

    global_data_dict = {
        'train': graphs_train,
        'val': graphs_val,
        'test': graphs_test}

    return global_data_dict, partition_dicts


def visualize_label_distribution_similarity_score(labels_of_all_clients):
    label_distribution_clients = []
    label_num = labels_of_all_clients[0][0]
    for client_idx in range(len(labels_of_all_clients)):
        labels_client_i = labels_of_all_clients[client_idx]
        sample_number = len(labels_client_i)
        active_property_count = [0.0] * label_num
        for sample_index in range(sample_number):
            label = labels_client_i[sample_index]
            for property_index in range(len(label)):
                # logging.info(label[property_index])
                if label[property_index] == 1:
                    active_property_count[property_index] += 1
        active_property_count = [float(active_property_count[i]) for i in range(len(active_property_count))]
        label_distribution_clients.append(copy.deepcopy(active_property_count))
    logging.info(label_distribution_clients)

    client_num = len(label_distribution_clients)
    label_distribution_similarity_score_matrix = np.random.random((client_num, client_num))

    for client_i in range(client_num):
        label_distribution_client_i = label_distribution_clients[client_i]
        for client_j in range(client_i, client_num):
            label_distribution_client_j = label_distribution_clients[client_j]
            logging.info(label_distribution_client_i)
            logging.info(label_distribution_client_j)
            a = np.array(label_distribution_client_i, dtype=np.float32)
            b = np.array(label_distribution_client_j, dtype=np.float32)

            from scipy import spatial
            distance = 1 - spatial.distance.cosine(a, b)
            label_distribution_similarity_score_matrix[client_i][client_j] = distance
            label_distribution_similarity_score_matrix[client_j][client_i] = distance
        # break
    logging.info(label_distribution_similarity_score_matrix)
    plt.title("Label Distribution Similarity Score")
    ax = sns.heatmap(label_distribution_similarity_score_matrix, annot=True, fmt='.3f')
    # # ax.invert_yaxis()
    # plt.show()


# Single process sequential
def load_partition_data(args, path, client_number, uniform=True, global_test=True, compact=True, normalize_features=False,
                        normalize_adj=False):
    global_data_dict, partition_dicts = partition_data_by_sample_size(args, path, client_number, uniform, compact=compact)

    data_local_num_dict = dict()
    train_data_local_dict = dict()
    val_data_local_dict = dict()
    test_data_local_dict = dict()

    collator = WalkForestCollator(normalize_features=normalize_features) if compact \
        else DefaultCollator(normalize_features=normalize_features, normalize_adj=normalize_adj)

    # This is a PyG Dataloader
    train_data_global = DataLoader(global_data_dict['train'], batch_size=args.batch_size, shuffle=True, collate_fn=collator,
                                        pin_memory=True)
    val_data_global = DataLoader(global_data_dict['val'], batch_size=args.batch_size, shuffle=True, collate_fn=collator,
                                      pin_memory=True)
    test_data_global =  DataLoader(global_data_dict['test'], batch_size=args.batch_size, shuffle=True, collate_fn=collator,
                                       pin_memory=True)

    train_data_num = len(global_data_dict['train'])
    val_data_num = len(global_data_dict['val'])
    test_data_num = len(global_data_dict['test'])

    for client in range(client_number):
        train_dataset_client = partition_dicts[client]['train']
        val_dataset_client = partition_dicts[client]['val']
        test_dataset_client = partition_dicts[client]['test']

        data_local_num_dict[client] = len(train_dataset_client)
        train_data_local_dict[client] = DataLoader(train_dataset_client, batch_size=args.batch_size, shuffle=True,
                                                        collate_fn=collator, pin_memory=True)
        val_data_local_dict[client] = DataLoader(val_dataset_client, batch_size=args.batch_size, shuffle=False,
                                                      collate_fn=collator, pin_memory=True)
        test_data_local_dict[client] = test_data_global if global_test else DataLoader(test_dataset_client,
                                                                                            batch_size=args.batch_size, shuffle=False,
                                                                                            collate_fn=collator,
                                                                                            pin_memory=True)

        logging.info("Client idx = {}, local sample number = {}".format(client, len(train_dataset_client)))

    return train_data_num, val_data_num, test_data_num, train_data_global, val_data_global, test_data_global, \
           data_local_num_dict, train_data_local_dict, val_data_local_dict, test_data_local_dict

