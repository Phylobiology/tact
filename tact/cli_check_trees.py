#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Try to assign tips to a pre-existing tree based on a taxonomy
# Jonathan Chang, May 13, 2016

from __future__ import division

import csv
import sys
import multiprocessing
import functools
import itertools
import math

import dendropy
import click

from .lib import get_monophyletic_node, get_birth_death_for_node, get_tree, get_tip_labels

def analyze_taxon(bb_tips, st_tips, backbone, simtaxed, taxon_node):
    taxon = taxon_node.label
    if not taxon:
        return None
    species = set([x.taxon.label for x in taxon_node.leaf_iter()])

    notes = []

    # does this clade even exist in the backbone?
    bb_species = species.intersection(bb_tips)
    if bb_species:
        bb_mrca = get_monophyletic_node(backbone, bb_species)
        if bb_mrca:
            bb_ntax = len(bb_mrca.leaf_nodes())
            bb_birth, bb_death = get_birth_death_for_node(bb_mrca, min(bb_ntax / len(species), 1))
            if bb_ntax > len(species):
                notes.append("BACKBONE clade has more tips than the taxonomy suggests")
        else:
            bb_ntax = bb_birth = bb_death = None
    else:
        bb_ntax = 0
        bb_birth = bb_death = bb_mrca = None

    st_mrca = get_monophyletic_node(simtaxed, species.intersection(st_tips))
    if st_mrca:
        st_ntax = len(st_mrca.leaf_nodes())
        st_birth, st_death = get_birth_death_for_node(st_mrca, min(st_ntax / len(species), 1))
        if st_ntax > len(species):
            notes.append("SIMULATED clade has more tips than the taxonomy suggests")
    else:
        st_ntax = st_birth = st_death = None

    if bool(bb_mrca) != bool(st_mrca) and bb_mrca is not None:
        notes.append("BACKBONE and SIMULATED trees differ in monophyly for this taxa")

    return [taxon, len(species), bb_ntax, st_ntax, bool(bb_mrca), bool(st_mrca), bb_birth, st_birth, bb_death, st_death, ", ".join(notes)]


@click.command()
@click.argument("simulated", type=click.Path(exists=True, dir_okay=False))
@click.option("--backbone", type=click.Path(exists=True, dir_okay=False), required=True, help="backbone phylogeny")
@click.option("--taxonomy", type=click.Path(exists=True, dir_okay=False), required=True, help="taxonomic phylogeny. Possibly created by `tact build_taxonic_tree`")
@click.option("--output", type=click.File("w"), help="Output CSV file report (defaults to standard output)", default="-")
@click.option("--cores", help="number of parallel cores to use", default=multiprocessing.cpu_count(), type=int)
@click.option("--chunksize", help="number of tree nodes to allocate to each core", type=int)
def main(simulated, backbone, taxonomy, output, cores, chunksize):
    """
    Check a SIMULATED phylogeny for consistency with its backbone source tree and a taxonomy.

    The SIMULATED phylogeny should have been generated by the tact add_taxa script.
    All phylogenies should be in Newick format.
    """
    pool = multiprocessing.Pool(processes=cores)
    click.echo("Using %d parallel cores" % cores, err=True)
    taxonomy = dendropy.Tree.get_from_path(taxonomy, schema="newick")
    tn = taxonomy.taxon_namespace
    click.echo("Taxonomy OK", err=True)

    r1 = pool.apply_async(get_tree, [backbone, tn])
    r2 = pool.apply_async(get_tree, [simulated, tn])

    backbone = r1.get()
    click.echo("Backbone OK", err=True)
    simulated = r2.get()
    click.echo("Simulated OK", err=True)

    bb_tips = get_tip_labels(backbone)
    st_tips = get_tip_labels(simulated)
    all_possible_tips = get_tip_labels(taxonomy)

    # Start calculating ASAP
    wrap = functools.partial(analyze_taxon, bb_tips, st_tips, backbone, simulated)
    nnodes = len(taxonomy.internal_nodes(exclude_seed_node=True))
    if chunksize is None:
        chunksize = max(5, math.ceil(nnodes / cores / 10))
    # We use preorder because the root is going to take the longest to
    # run calculations. Allocating things to cores takes a non-negigible
    # amount of time so we want the root to be running for the longest.
    it = pool.imap_unordered(wrap, taxonomy.preorder_internal_node_iter(exclude_seed_node=True), chunksize=chunksize)


    writer = csv.writer(output)
    writer.writerow("node taxonomy_tips backbone_tips simulated_tips backbone_monophyletic simulated_monophyletic backbone_birth simulated_birth backbone_death simulated_death warnings".split())

    if cores > 1:
        click.echo("Checking %d nodes with %d nodes per core" % (nnodes, chunksize))
    else:
        click.echo("Checking %d nodes" % nnodes)

    with click.progressbar(it, length=nnodes) as prog:
        for result in prog:
            if result:
                writer.writerow(result)

if __name__ == '__main__':
    main()
