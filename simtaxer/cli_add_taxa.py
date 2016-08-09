#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Try to assign tips to a pre-existing tree based on a taxonomy
# Jonathan Chang, May 13, 2016

from __future__ import division

import csv
import itertools
import functools
import collections
import sys
import random
from math import log, exp, ceil
import math
from decimal import Decimal as D
import multiprocessing
import operator
from Queue import PriorityQueue
from time import time
from contextlib import contextmanager

import dendropy
import click

import logging
logger = logging.getLogger(__name__)

from .lib import optim_bd, is_binary, get_short_branches, get_tip_labels, get_monophyletic_node, crown_capture_probability, edge_iter, get_new_times

global invalid_map
invalid_map = {}
def search_ancestors_for_valid_backbone_node(taxonomy_node, backbone_tips, ccp):
    global mrca
    global invalid_map
    seen = []
    target_node = None
    for anc in taxonomy_node.ancestor_iter():
        if anc.label in invalid_map:
            logger.debug("cache HIT on invalid_map for {} ({} => {})".format(taxonomy_node.label, anc.label, invalid_map[anc.label].label))
            anc = invalid_map[anc.label]
        full_tax = get_tip_labels(anc)
        extant_tax = full_tax.intersection(backbone_tips)
        backbone_node = mrca.get(extant_tax)
        seen.append(anc.label)
        if backbone_node is None:
            logger.debug("...{} not monophyletic...".format(anc.label))
        elif crown_capture_probability(len(full_tax), len(extant_tax)) < ccp:
            logger.debug("...{} fails crown threshold ({} < {})...".format(anc.label, crown_capture_probability(len(full_tax), len(extant_tax)), ccp))
        else:
            taxonomy_target = anc
            backbone_target = backbone_node
            logger.debug("...got valid node: {}".format(taxonomy_target.label))
            break
    else:
        logger.warning("couldn't find valid taxonomy node in ancestor chain for {} ({})".format(taxonomy_node.label, " => ".join(seen)))
        return None
    seen.pop() # ignore last node
    for x in seen:
        invalid_map[x] = taxonomy_target
    return (taxonomy_target, backbone_target)

def get_birth_death_rates(node, sampfrac):
    return optim_bd(get_ages(node), sampfrac)

def get_ages(node):
    ages = [x.age for x in node.ageorder_iter(include_leaves=False, descending=True)]
    ages += [node.age]
    return ages

def get_new_branching_times2(backbone_node, taxonomy_node, backbone_tree, told=None, tyoung=0, min_ccp=0.8, num_new_times=None):
    """
    Get `n_total` new branching times for a `node`.
    """
    original_backbone_node = backbone_node
    original_taxonomy_node = taxonomy_node
    n_extant = len(original_backbone_node.leaf_nodes())
    n_total = len(original_taxonomy_node.leaf_nodes())
    if num_new_times is None:
        num_new_times = n_total - n_extant
    new_ccp = ccp = crown_capture_probability(n_total, n_extant)
    # If we have a single or doubleton then go up the taxonomy to get a
    # new node with hopefully better sampling
    while n_extant <= 2 or new_ccp < min_ccp:
        logger.debug("backtracking from {} due to poor sampling".format(taxonomy_node.label))
        taxonomy_node, backbone_node = search_ancestors_for_valid_backbone_node(taxonomy_node, get_tip_labels(backbone_tree), ccp=min_ccp)
        n_extant = len(backbone_node.leaf_nodes())
        n_total = len(taxonomy_node.leaf_nodes())
        new_ccp = crown_capture_probability(n_total, n_extant)
    sampling = n_extant / n_total
    if backbone_node.annotations.get_value("birth"):
        #logger.debug("cache hit on b/d rates for {}".format(taxonomy_node.label))
        birth = backbone_node.annotations.get_value("birth")
        death = backbone_node.annotations.get_value("death")
    else:
        logger.debug("cache MISS on b/d rates for {}".format(taxonomy_node.label))
        birth, death = get_birth_death_rates(backbone_node, sampling)
        backbone_node.annotations.add_new("birth", birth)
        backbone_node.annotations.add_new("death", death)
    if ccp < min_ccp and told is not None:
        told = original_backbone_node.parent_node.age
    if len(original_backbone_node.leaf_nodes()) == 1 and told is None:
        # attach to stem in the case of a singleton
        told = original_backbone_node.parent_node.age
    times = get_new_times(get_ages(original_backbone_node), birth, death, num_new_times, told, tyoung)
    return birth, death, ccp, times

def get_new_branching_times(node, n_extant, n_total, told=None, tyoung=0, min_ccp=0.8):
    """
    Get `n_total` new branching times for a `node`.
    """
    if n_extant == n_total:
        raise Exception("get_new_branching_times args 2 and 3 cannot be equal")
    ccp = crown_capture_probability(n_total, n_extant)
    if n_extant == 1:
        # if we have a singleton then go up a node to get a better handle on
        # birth/death rates and origination times
        node = node.parent_node
        diff = n_total - n_extant
        n_extant = len(node.leaf_nodes())
        n_total = n_extant + diff
    ages = [x.age for x in node.ageorder_iter(include_leaves=False, descending=True)]
    ages += [node.age]
    sampling = n_extant / n_total
    if node.annotations.get_value("birth"):
        birth = node.annotations.get_value("birth")
        death = node.annotations.get_value("death")
    else:
        birth, death = optim_bd(ages, sampling)
        node.annotations.add_new("birth", birth)
        node.annotations.add_new("death", death)
    if ccp < min_ccp and told is not None:
        told = node.parent_node.age
    return birth, death, ccp, get_new_times(ages, birth, death, n_total - n_extant, told, tyoung)

def fill_new_taxa(namespace, node, new_taxa, times, stem=False, excluded_nodes=None):
    if stem:
        node = node.parent_node

    for new_species, new_age in itertools.izip(new_taxa, times):
        new_node = dendropy.Node()
        new_node.annotations.add_new("creation_method", "fill_new_taxa")
        new_node.age = new_age
        new_leaf = new_node.new_child(taxon=namespace.require_taxon(new_species), edge_length=new_age)
        new_leaf.age = 0
        node = graft_node(node, new_node, stem)

    if list(get_short_branches(node)):
        logger.warn("{} short branches detected".format(len(list(get_short_branches(node)))))

    node.locked = None

    return node

def graft_node(graft_recipient, graft, stem=False):
    """
    Grafts a node `graft` randomly in the subtree below node
    `graft_recipient`. The attribute `graft.age` must be set so
    we know where is the best place to graft the node. The node
    `graft` can optionally have child nodes, in this case the 
    `edge.length` attribute should be set on all child nodes if
    the tree is to remain ultrametric.
    """

    # We graft things "below" a node by picking one of the children
    # of that node and forcing it to be sister to the grafted node
    # and adjusting the edge lengths accordingly. Therefore, the node
    # *above* which the graft lives (i.e., the one that will be the child
    # of the new graft) must fulfill the following requirements:
    #
    # 1. Must not be the crown node (cannot graft things above crown node)
    # 2. Must be younger than the graft node (no negative branches)
    # 3. Seed node must be older than graft node (no negative branches)
    # 4. Must not be locked (intruding on monophyly)
    def filter_fn(x):
        return x.head_node.age <= graft.age and x.head_node.parent_node.age >= graft.age and x.label != "locked"
    all_edges = list(edge_iter(graft_recipient))
    if stem:
        # also include the crown node's subtending edge
        all_edges.append(graft_recipient.edge)
    eligible_edges = [x for x in all_edges if filter_fn(x)]

    if not eligible_edges:
        raise Exception("could not place node {} in clade {}".format(graft, graft_recipient))
    focal_node = random.choice([x.head_node for x in eligible_edges])
    seed_node = focal_node.parent_node
    sisters = focal_node.sibling_nodes()

    # pick a child edge and detach its corresponding node
    #
    # DendroPy's Node.remove_child() messes with the edge lengths.
    # But, Node.clear_child_nodes() simply cuts that bit of the tree out.
    seed_node.clear_child_nodes()

    # set the correct edge length on the grafted node and make the grafted
    # node a child of the seed node
    graft.edge.length = seed_node.age - graft.age
    if graft.edge.length < 0:
        raise Exception("negative branch length")
    sisters.append(graft)
    seed_node.set_child_nodes(sisters)

    # make the focal node a child of the grafted node and set edge length
    focal_node.edge.length = graft.age - focal_node.age
    if focal_node.edge.length < 0:
        raise Exception("negative branch length")
    graft.add_child(focal_node)

    # return the (potentially new) crown of the clade
    if graft_recipient.parent_node == graft:
        return graft
    return graft_recipient

def create_clade(namespace, species, ages):
    tree = dendropy.Tree(taxon_namespace=namespace)
    species = list(species)
    ages.sort(reverse=True)
    # need to generate the "stem node"
    tree.seed_node.age = ages.pop(0)
    # clade of size 1?
    if not ages:
        node = tree.seed_node.new_child(edge_length=tree.seed_node.age, taxon=namespace.require_taxon(species[0]))
        node.age = 0.0
        [x.annotations.add_new("creation_method", "create_clade") for x in tree.preorder_node_iter()]
        return tree
    node = tree.seed_node.new_child()
    node.age = ages.pop(0)
    for age in ages:
        valid_nodes = [x for x in tree.nodes() if len(x.child_nodes()) < 2 and age < x.age and x != tree.seed_node]
        assert len(valid_nodes) > 0
        node = random.sample(valid_nodes, 1).pop()
        child = node.new_child()
        child.age = age
    n_species = len(species)
    random.shuffle(species)
    for node in tree.preorder_node_iter(filter_fn=lambda x: x.age > 0 and x != tree.seed_node):
        while len(node.child_nodes()) < 2 and len(species) > 0:
            new_species = species.pop()
            new_leaf = node.new_child(taxon=namespace.require_taxon(new_species))
            new_leaf.age = 0.0
    assert n_species == len(tree.leaf_nodes())
    assert len(tree.seed_node.child_nodes()) == 1
    [x.annotations.add_new("creation_method", "create_clade") for x in tree.preorder_node_iter()]
    assert is_binary(tree.seed_node.child_nodes()[0])
    tree.set_edge_lengths_from_node_ages(error_on_negative_edge_lengths=True)
    lock_clade(tree.seed_node)
    if list(get_short_branches(tree.seed_node)):
        logger.warn("{} short branches detected".format(len(list(get_short_branches(tree.seed_node)))))
    return tree

def lock_clade(node):
    for edge in edge_iter(node):
        edge.label = "locked"

def is_fully_locked(node):
    return all([x.label == "locked" for x in edge_iter(node)])

def get_min_age(node):
    try:
        return min([x.head_node.age for x in edge_iter(node) if x.label is not "locked"])
    except ValueError:
        return 0.0

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    l = list(l)
    for i in xrange(0, len(l), n):
        yield l[i:i + n]

def _fastmrca_getter(tn, x):
    taxa = tn.get_taxa(labels=x)
    bitmask = 0L
    for taxon in taxa:
        bitmask |= tn.taxon_bitmask(taxon)
    return bitmask

## GLOBALS
global mrca

class FastMRCA(object):
    def __init__(self, tree, max_singlethread_taxa=None, cores=multiprocessing.cpu_count()):
        self.tree = tree
        self.cores = cores
        self.pool = multiprocessing.Pool(processes=cores)
        self.maxtax = max_singlethread_taxa
        if self.maxtax is None:
            self.maxtax = self.autotune()
            logger.info("Autotuned parameters: single-thread cutoff is {}".format(self.maxtax))

    def autotune(self):
        tn = self.tree.taxon_namespace
        ntax = self.cores * self.cores
        while True:
            if ntax > len(tn):
                return len(tn)
            st = []
            mt = []
            for i in range(3):
                labels = [tx.label for tx in random.sample(tn, ntax)]
                start_time = time()
                tn.taxa_bitmask(labels=labels)
                st.append(time() - start_time)
                start_time = time()
                self.bitmask(labels)
                mt.append(time() - start_time)
            # Get median times
            st_s = st[1]
            mt_s = mt[1]
            if mt_s - st_s < 0.75:
                # Single-thread performs ~0.75s worse
                logger.debug("maxtax={} st={} mt={}".format(ntax, st_s, mt_s))
                return ntax
            else:
                logger.debug("maxtax={} st={} mt={}".format(ntax, st_s, mt_s))
                ntax = ntax * 4

    def bitmask(self, labels):
        tn = self.tree.taxon_namespace
        if len(labels) < self.maxtax:
            return tn.taxa_bitmask(labels=labels)
        start_time = time()
        f = functools.partial(_fastmrca_getter, tn)
        full_bitmask = 0L
        for res in self.pool.map(f, chunks(labels, int(ceil(len(labels) / self.cores))), chunksize=1):
            full_bitmask |= res
        logger.debug("parallel fastMRCA: n={}, t={:.1f}s".format(len(labels), time() - start_time))
        return full_bitmask

    def get(self, labels):
        mrca = self.tree.mrca(taxon_labels=labels)
        labels = set(labels)
        if not mrca:
            return None
        if mrca and labels.issuperset(get_tip_labels(mrca)):
            return mrca

    def __del__(self):
        self.pool.terminate()

def process_node(backbone_tree, backbone_bitmask, all_possible_tips, taxon_node):
    taxon = taxon_node.label
    if not taxon:
        # ignore unlabeled ranks
        return None
    species = get_tip_labels(taxon_node)
    all_bitmask = backbone_tree.taxon_namespace.taxa_bitmask(labels=species)
    extant_bitmask = all_bitmask & backbone_bitmask
    mrca = backbone_tree.mrca(leafset_bitmask=extant_bitmask)
    if mrca:
        birth, death = get_birth_death_rates(mrca, len(mrca.leaf_nodes()) / len(taxon_node.leaf_nodes()))
        return (taxon_node, all_bitmask, birth, death)
    else:
        return (taxon_node, None, None, None)

def run_precalcs2(taxonomy_tree, backbone_tree, min_ccp=0.8, min_extant=3, cores=None):
    global mrca
    tree_tips = get_tip_labels(backbone_tree)
    backbone_bitmask = mrca.bitmask(tree_tips)
    all_possible_tips = get_tip_labels(taxonomy_tree)
    nnodes = len(taxonomy_tree.internal_nodes(exclude_seed_node=True))
    start_time = time()
    with click.progressbar(taxonomy_tree.preorder_internal_node_iter(exclude_seed_node=True), label="Calculating bd rates", length=nnodes, show_pos=True) as progress:
        for taxon_node in progress:
            taxon = taxon_node.label
            if not taxon:
                continue
            species = get_tip_labels(taxon_node)
            all_bitmask = mrca.bitmask(species)
            extant_bitmask = all_bitmask & backbone_bitmask
            mrca_node = backbone_tree.mrca(leafset_bitmask=extant_bitmask)
            if mrca_node:
                birth, death = get_birth_death_rates(mrca_node, len(mrca_node.leaf_nodes()) / len(taxon_node.leaf_nodes()))
                if birth is not None and death is not None:
                    mrca_node.annotations.add_new("birth", birth)
                    mrca_node.annotations.add_new("death", death)
    logger.info("time elapsed: {:.1f} seconds".format(time() - start_time))

def run_precalcs(taxonomy_tree, backbone_tree, min_ccp=0.8, min_extant=3, cores=multiprocessing.cpu_count()):
    global mrca
    tree_tips = get_tip_labels(backbone_tree)
    backbone_bitmask = mrca.bitmask(tree_tips)
    all_possible_tips = get_tip_labels(taxonomy_tree)

    nnodes = len(taxonomy_tree.internal_nodes(exclude_seed_node=True))

    tips_per_node = [(len(x.leaf_nodes()), x) for x in taxonomy_tree.preorder_internal_node_iter(exclude_seed_node=True)]
    buckets = []
    queue = PriorityQueue()
    for x in range(max(int(cores/4), 2)):
        buckets.append([])
        queue.put((0, x))
    sums = [0] * cores

    for ntips, node in sorted(tips_per_node, key=operator.itemgetter(1), reverse=True):
        _, i = queue.get()
        buckets[i].append(node)
        sums[i] += ntips
        queue.put((sums[i], i))

    buckets.sort(key=lambda x: len(x))

    logger.debug("Parallel worker assignments ({} cores): {}".format(len(buckets), [len(x) for x in buckets]))

    with click.progressbar(label="Calculating birth/death rates", length=nnodes, show_pos=True) as progress:
        full_results = []
        def cb(results):
            full_results.extend(results)
            progress.update(len(results))
        fn = functools.partial(process_node, backbone_tree, backbone_bitmask, all_possible_tips)
        #pool = multiprocessing.Pool(cores)
        start_time = time()
        promises = []
        for acc_nodes in buckets:
            promises.append(mrca.pool.map_async(fn, acc_nodes, chunksize=len(acc_nodes), callback=cb))
        [x.wait() for x in promises]
        #pool.close()
        #pool.join()
        for result in full_results:
            if result is not None:
                taxon_node, taxon_bitmask, birth, death = result
                if birth is not None and death is not None:
                    backbone_node = backbone_tree.mrca(leafset_bitmask=taxon_bitmask & backbone_bitmask)
                    if backbone_node:
                        backbone_node.annotations.add_new("birth", birth)
                        backbone_node.annotations.add_new("death", death)
    diff = time() - start_time
    if diff > 5:
        logger.info("time elapsed: {:.1f} seconds".format(diff))

@click.command()
@click.option("--taxonomy", help="a taxonomy tree", type=click.File("rb"), required=True)
@click.option("--backbone", help="the backbone tree to attach the taxonomy tree to", type=click.File("rb"), required=True)
@click.option("--outgroups", help="comma separated list of outgroup taxa to ignore")
@click.option("--output", required=True, help="output base name to write out")
@click.option("--min-ccp", help="minimum probability to use to say that we've sampled the crown of a clade", default=0.8)
@click.option("--cores", help="number of cores to use for parallel operations", type=int)
@click.option("-v", "--verbose", help="emit extra information (can be repeated)", count=True)
@click.option("--log", "log_file", help="if verbose output is enabled, send it to this file instead of standard output")
def main(taxonomy, backbone, outgroups, output, min_ccp, cores, verbose, log_file):
    """
    Add tips onto a BACKBONE phylogeny using a TAXONOMY phylogeny.
    """
    global mrca

    if verbose >= 2:
        logger.setLevel(logging.DEBUG)
    elif verbose == 1:
        logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.WARNING)
    if log_file:
        logger.addHandler(logging.FileHandler(log_file))
    else:
        logger.addHandler(logging.StreamHandler())



    logger.info("reading taxonomy")
    taxonomy = dendropy.Tree.get_from_stream(taxonomy, schema="newick")
    tn = taxonomy.taxon_namespace
    tn.is_mutable = True
    if outgroups:
        outgroups = [x.replace("_", " ") for x in outgroups.split(",")]
        tn.new_taxa(outgroups)
    tn.is_mutable = False

    logger.info("reading tree")
    tree = dendropy.Tree.get_from_stream(backbone, schema="newick", rooting="force-rooted", taxon_namespace=tn)
    tree.encode_bipartitions()
    tree.calc_node_ages()

    tree_tips = get_tip_labels(tree)
    all_possible_tips = get_tip_labels(taxonomy)

    logger.info("{} tips to add".format(len(tree_tips.symmetric_difference(all_possible_tips))))

    full_clades = set()

    if cores is None:
        cores = multiprocessing.cpu_count()

    mrca = FastMRCA(tree, max_singlethread_taxa=cores*cores*4, cores=cores)
    logger.debug("fastmrca loaded")

    run_precalcs(taxonomy, tree, min_ccp, cores=cores)

    initial_length = len(tree_tips)

    def isf(x):
        if x:
            return x.label
        else:
            return ""

    with click.progressbar(taxonomy.postorder_internal_node_iter(exclude_seed_node=True), label="Adding taxa to tree", length=len(all_possible_tips) - initial_length, show_pos=True, item_show_func=isf) as bar:
        for taxon_node in bar:
            taxon = taxon_node.label
            if not taxon:
                continue
            spaces = taxon_node.level() * "  "
            species = get_tip_labels(taxon_node)
            extant_species = tree_tips.intersection(species)
            logger.info("{}{} ({}/{})... ({} remain)".format(spaces, taxon, len(extant_species), len(species), len(all_possible_tips) - len(tree_tips)))

            clades_to_generate = full_clades.intersection([x.label for x in taxon_node.postorder_internal_node_iter(exclude_seed_node=True)])
            to_remove = set([])

            if extant_species:
                if extant_species == species:
                    logger.debug("{}  => all species accounted for".format(spaces))
                    continue
                if tree_tips.issuperset(species):
                    logger.info("{}  => all species already present in tree".format(spaces))
                    continue

                node = mrca.get(extant_species)
                if not node:
                    logger.info(spaces + "  => not monophyletic")
                    continue

                clade_sizes = [(clade, len(taxonomy.find_node_with_label(clade).leaf_nodes())) for clade in clades_to_generate]

                # sorting clades by size should add genera before families... better way would be to sort by rank
                for clade, clade_size in sorted(clade_sizes, key=operator.itemgetter(1)):
                    full_node = taxonomy.find_node_with_label(clade)
                    full_node_species = get_tip_labels(full_node)
                    if tree_tips.issuperset(full_node_species):
                        logger.info("{}  => skipping {} as all species already present in tree".format(spaces, clade))
                        full_clades.remove(clade)
                        continue
                    #birth, death, ccp, times = get_new_branching_times(node, len(species), len(species) + len(full_node_species), tyoung=get_min_age(node), min_ccp=min_ccp)
                    birth, death, ccp, times = get_new_branching_times2(node, taxon_node, tree, tyoung=get_min_age(node), min_ccp=min_ccp, num_new_times=len(full_node_species))
                    logger.info("{}  => adding {} (n={})".format(spaces, clade, clade_size))
                    #logger.info("b {} => {}, d {} => {}, ccp {} => {}, times {} => {}".format(birth1, birth, death1, death, ccp1, ccp, times1, times))

                    if is_fully_locked(node):
                        logger.info("{}  => {} is fully locked, attaching to stem".format(spaces, taxon))
                        # must attach to stem for this clade, so generate a time on the stem lineage
                        _, _, _, times2 = get_new_branching_times2(node, taxon_node, tree, min_ccp=min_ccp, told=node.parent_node.age, tyoung=node.age, num_new_times=1)
                        #_, _, _, times2 = get_new_branching_times(node, len(species), len(species) + 1, told=node.parent_node.age, tyoung=node.age, min_ccp=min_ccp)
                        times.sort()
                        times.pop()
                        times.append(times2.pop())

                    # generate a new tree
                    new_tree = create_clade(tn, full_node_species, times)
                    node = graft_node(node, new_tree.seed_node, is_fully_locked(node))
                    tree.calc_node_ages()
                    tree.update_bipartitions()
                    tree_tips = get_tip_labels(tree)
                    bar.pos = len(tree_tips) - initial_length; bar.update(0)
                    extant_species = tree_tips.intersection(species)
                    full_clades.remove(clade)
                    assert(is_binary(node))

                # check to see if we need to continue adding species
                if extant_species == species:
                    # lock clade since it is monophyletic and filled
                    lock_clade(node)
                    continue
                if len(extant_species) == len(species):
                    raise Exception("enough species are present but mismatched?")

                logger.info("{}  => adding {} new species".format(spaces, len(species.difference(extant_species))))
                node = mrca.get(extant_species)
                birth1, death1, ccp1, times1 = get_new_branching_times(node, len(extant_species), len(species), tyoung=get_min_age(node), min_ccp=min_ccp)
                birth, death, ccp, times = get_new_branching_times2(node, taxon_node, tree, tyoung=get_min_age(node), min_ccp=min_ccp)
                #logger.debug("b {} => {}, d {} => {}, ccp {} => {}, times {} => {}".format(birth1, birth, death1, death, ccp1, ccp, times1, times))
                fill_new_taxa(tn, node, species.difference(tree_tips), times, ccp < min_ccp)
                tree.update_bipartitions()
                tree.calc_node_ages()
                tree_tips = get_tip_labels(tree)
                bar.pos = len(tree_tips) - initial_length; bar.update(0)
                # since only monophyletic nodes get to here, lock this clade
                lock_clade(node)
                assert(is_binary(node))
            else:
                # create clade from whole cloth
                full_clades.add(taxon)
            bar.pos = len(tree_tips) - initial_length; bar.update(0)

    del mrca
    assert(is_binary(tree.seed_node))
    tree.ladderize()
    for leaf in tree.leaf_node_iter():
        if leaf.edge.length <= 0.001:
            logger.info("warning: taxon {} has extremely short branch ({})".format(leaf.taxon.label, leaf.edge.length))
    tree.write(path=output + ".newick.tre", schema="newick")
    tree.write(path=output + ".nexus.tre", schema="nexus")

if __name__ == '__main__':
    main()

"""
tmp = tree.extract_tree_with_taxa([x.taxon for x in node.leaf_iter()])
tmp.write_to_path("tmp.tre", schema="newick")
"""
