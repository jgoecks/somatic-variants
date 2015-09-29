"""
Library for using GEMINI's API for somatic variant calling.
"""

import argparse
import os
import itertools
import time

import pandas as pd
from toyplot import Canvas, browser
from collections import namedtuple
from gemini import GeminiQuery

# Default list of columns to include in query.
DEFAULT_VAR_COLS = ["variant_id", "type", "gene", "chrom", "start", "ref", "alt", "HP", "num_het", \
                    "max_aaf_all", "impact", "impact_severity", "cosmic_ids", "rs_ids", \
                    "sift_pred", "polyphen_pred"]
COMMON_DATABASES = ["1kg", "exac", "esp"]
DEFAULT_VARIANT_QUERY = "SELECT %s FROM variants" % ",".join(DEFAULT_VAR_COLS)

# ********
# General GEMINI functions.
# ********

def query_sample_het(gemini_db, sample, cols="chrom, start, end, ref, alt, gene, cosmic_ids", min_het_count=0, addl_gt_filter=None):
    """
    Query database, returning columns + sample genotype for variants that are (a) HET for sample;
    (b) have a minimum number of hets total; and (c) meet additional genotype filters.
    """
    query = "select %s, gts.%s from variants" % (cols, sample)
    gt_filter = "gt_types.%s == HET and (gt_types).(*).(==HET).(count >= %i)" % (sample, min_het_count)
    if addl_gt_filter:
        gt_filter += ' and %s' % addl_gt_filter
    return get_query_results(gemini_db, query, gt_filter)

def get_samples(gemini_db):
    """
    Returns list of samples in a GEMINI database.
    """
    return [str(sample['name']) for sample in get_query_results(gemini_db, "select name from samples")]

def has_sample(gemini_db, sample):
    return int( str( get_query_results(gemini_db, "select count(*) from samples where name='%s'" % sample).next() ) ) != 0

def get_query_results(gemini_db, query, gt_filter="", as_dataframe=False):
    """
    Returns results of query.
    """

    gq = GeminiQuery(gemini_db)
    gq.run(query, gt_filter=gt_filter)

    if as_dataframe:
        # Return results as dataframe.
        df = pd.DataFrame([str(row).split('\t') for row in gq], columns=gq.header.split('\t'))
        return df
    else:
        # Return results as iterator.
        return gq

def get_gt_filter(sample, gt_type):
    """
    Returns clause for filtering variants based on sample and GT type.
    """
    return "gt_types.%s == %s" % (sample, gt_type)

def get_gt_count_filter(gt_type, count):
    """
    Returns clause for filtering genotypes based on type and count.
    """
    return "(gt_types).(*).(==%s).(count > %i)" % (gt_type, count)

def get_in_and_aff_clause(db, aaf=0.01):
    """
    Returns clause for checking if variant is below a certain alternate allele frequency in a database
    either because the variant is not in the database or its AAF is below the threshold.
    """
    return "NOT in_%s OR aaf_%s_all < %0.3f" % (db, db, aaf)

def get_no_common_vars_clause(aaf=0.01):
    """
    Returns clause for removing common variants from a query.
    """
    return ' AND '.join( ['(%s)' % get_in_and_aff_clause(db, aaf) for db in COMMON_DATABASES ] )

def get_annotation_clause(anno_col_name, has_anno=True):
    val = 1
    if not has_anno:
        val = 0
    return "%s = %i" % (anno_col_name, val)

def get_annotation_and_no_common_clause(anno_col_name, has_anno=True, aaf=0.01):
    """
    Returns clause for selecting variants with or without an annotation
    but not appearing in common databases at a given alternate allele frequency.
    """
    val = 1
    if not has_anno:
        val = 0
    return '%s AND %s' % ( get_annotation_clause(anno_col_name, val), get_no_common_vars_clause() )

def get_novel_query(annotations, var_type='snp', allele_freq=0.1):
    """
    Return query to get novel variants.
    """

    add_clause = ""
    if var_type is 'snp':
        #TODO: be more precise about which impacts in HIGH (e.g., stop_gain, stop_loss) and MED (e.g. non-synonymous)
        # should be selected
        # Add clause for novel SNPs to require high impact or med impact + SIFT/PolyPhen to be true.
        add_clause = "AND (impact_severity = 'HIGH' or (impact_severity = 'MED' AND " \
                     "sift_pred = 'deleterious' AND polyphen_pred = 'probably_damaging'))"

    # Set up query.
    no_anno = [get_annotation_clause(anno, has_anno=False) for anno in annotations]
    return "%s WHERE (type = '%s') AND (allele_bal >= %f) AND %s AND %s %s" \
           % ( DEFAULT_VARIANT_QUERY, var_type, allele_freq, " AND ".join(no_anno), get_no_common_vars_clause(), add_clause)

def get_hotspot_variants(annotation, allele_bal=0.02):
    """
    Returns query to select variants in a hotspot, i.e. those with an annotation and minimum allele_bal
    """
    return "%s WHERE (allele_bal >= %f) AND %s" % \
          ( DEFAULT_VARIANT_QUERY, allele_bal, get_annotation_and_no_common_clause(annotation) )

# ********
# Variant dataframe functions.
# ********

def add_gt_attrs_cols(variants_dataframe):
    """
    Returns the dataframe with: (a) num_het adjusted for uncalled HETs; and
    (b) additional columns for mean allele_freq, alt_depth, and depth.
    """

    def get_cols_df(df, cols_prefix):
        """
        Returns a dataframe with all columns that have a given prefix.
        """
        series = pd.Series(df.columns.str.startswith(cols_prefix))
        return df[ series[series == True].index ]

    # Genotype attributes prefixes.
    prefixes = ["gt_quals", "gt_depths", "gt_alt_depths"]


    # Get dataframe of genotypes, attributes.
    gts_df = get_cols_df(variants_dataframe, "gts.")
    gt_attrs_dfs = [get_cols_df(variants_dataframe, prefix) for prefix in prefixes]

    # Iterate through rows/variants and count the number of samples with variants that meet
    # the allele frequency threshold.
    num_het = []
    mean_gt_attrs = [ [] for prefix in prefixes ]
    for i, gts_row in gts_df.iterrows():
        # Get IDs for heterozygous samples.
        gts_row_bool = gts_row.str.endswith('/.') # False for REF/ALT, True for ./. or REF/.
        het_samples = gts_row_bool[gts_row_bool == False].index # Get names of het samples.
        cur_num_het = len(het_samples)

        #print variants_dataframe.iloc[i]['variant_id'], het_samples

        # Compute cur means for each attribute.
        if cur_num_het == 0:
            cur_means = [0 for prefix in prefixes]
        else:
            cur_means = []
            for j, prefix in enumerate(prefixes):
                # Using attribute DF, filter for HET samples and get mean.
                gt_attr_df = gt_attrs_dfs[j]
                #print "****", len(gt_attr_df.iloc[i][ het_samples.str.replace('gts', prefix) ])
                #print gt_attr_df.iloc[i][ het_samples.str.replace('gts', prefix) ]
                cur_means.append( gt_attr_df.iloc[i][ het_samples.str.replace('gts', prefix) ].mean() )


        # Set mean, attributes.
        num_het.append(cur_num_het)
        for j, prefix in enumerate(prefixes):
            mean_gt_attrs[j].append(cur_means[j])

    # Update and add columns to variants dataframe.
    variants_dataframe['num_het'] = pd.Series(num_het)
    for i, prefix in enumerate(prefixes):
        variants_dataframe['mean_' + prefix] = pd.Series(mean_gt_attrs[i])

    # Show scatterplot of num_het vs. mean_af
    #canvas = Canvas(width=500, height=500)
    #axes = canvas.axes(label="Test", xlabel="counts", ylabel="mean AF")
    #mark = axes.scatterplot(num_het, mean_af)
    #browser.show(canvas)

    return variants_dataframe

def get_genotypes_df(gemini_db, annotations, no_common=True, min_alt_depth=5, min_depth=50, min_anno_af=0.02, min_novel_af=0.10):
    """
    Returns a dataframe with variant attributes and genotype information-genotype depths, genotype alt depths, genotype quals-
    for all genotypes that meet the given criteria. Genotypes that do not match criteria are removed from the dataframe by setting
    the allele to './.' and attributes to -1
    """

    # Get dataframe with all variant attributes, all sample genotypes, genotype depths, genotype alt depths, genotype quals.
    cols = DEFAULT_VAR_COLS + annotations
    query = "SELECT %s, (gts).(*), (gt_depths).(*), (gt_alt_depths).(*), (gt_quals).(*) FROM variants" % ",".join(cols)
    if no_common:
        query += " WHERE max_aaf_all < 0.01"

    # Get initial dataframe of query results.
    df = get_query_results(gemini_db, query, as_dataframe=True).convert_objects(convert_numeric=True)

    # Clear samples that do not meet min depth/alt depth/allele frequency.
    df = filter_genotypes_in_samples(df, get_samples(gemini_db), annotations, \
                                     min_depth=min_depth, min_alt_depth=min_alt_depth, \
                                     min_anno_af=min_anno_af, min_novel_af=min_novel_af)

    # Add/update genotype attribute columns.
    df = add_gt_attrs_cols(df)

    return df

def get_somatic_vars_in_sample(gemini_db, annotations, sample_name, alt_depth=10, depth=50, hotspot_af=0.02, novel_af=0.10):
    """
    Returns all somatic variants in a single sample.
    """

    var_cols = ["variant_id", "chrom", "start", "ref", "alt", "gene", "cosmic_ids", "HP", "codon_change", "aa_change", "rs_ids", "impact"] + annotations
    all_cols = ["sample"] + var_cols + ["genotype", "allele_freq", "alt_depth", "depth"]

    # Get dataframe
    query = "SELECT *, gts.%(sample)s, gt_depths.%(sample)s, gt_alt_depths.%(sample)s, gt_quals.%(sample)s FROM variants WHERE max_aaf_all < 0.01" % { 'sample': sample_name }
    gt_filter = "gt_quals.%(sample)s >= %(hotspot_af)f AND \
                 gt_alt_depths.%(sample)s >= %(alt_depth)i AND \
                 gt_depths.%(sample)s >= %(depth)i AND \
                 (gt_types.%(sample)s == HET OR gt_types.%(sample)s == HOM_ALT)" \
                 % { 'sample': sample_name, 'hotspot_af': hotspot_af, 'alt_depth': alt_depth, 'depth': depth }

    df = get_query_results(gemini_db, query, gt_filter=gt_filter, as_dataframe=True).convert_objects(convert_numeric=True)

    # Filter out 'REF/.' heterozygous.
    df = df[df['gts.' + sample_name].str.endswith(('A', 'C', 'T', 'G'))]

    # Select annotation columns and count number of times each variant is annotated.
    counts = df[ df[annotations] == 1].count(1)

    # Select variants that have one or more annotations.
    annotated_vars = df.loc[counts[counts != 0].index]
    #print annotated_vars

    # TODO: add novel variants to index.

    # Filter for somatic variants and select columns.
    somatic_vars_subset_cols = annotated_vars[var_cols + \
                                 ["gts." + sample_name, "gt_quals." + sample_name, "gt_alt_depths." + sample_name, "gt_depths." + sample_name]]

    # Add sample column.
    somatic_vars_subset_cols.insert(0, "sample", pd.Series())
    somatic_vars_subset_cols["sample"] = sample_name

    # Rename columns to generic names.
    somatic_vars_subset_cols.columns = all_cols

    return somatic_vars_subset_cols

def get_somatic_vars_by_sample2(gemini_db, annotations, samples=None, hotspot_af=0.0):
    """
    Returns a dataframe for all somatic variants in the database with the following columns:
    sample, chrom, start, ref, alt, gene, cosmic_ids, HP, num_het, mean_af, genotype, allele_freq, alt_depth, depth
    """

    if not samples:
        samples = get_samples(gemini_db)

    # Initialize dataframe.
    somatic_vars_df = get_somatic_vars_in_sample(gemini_db, annotations, samples[0], hotspot_af=hotspot_af)

    for sample in samples[1:]:
        # Get somatic variants for sample.
        sample_somatic_vars = get_somatic_vars_in_sample(gemini_db, annotations, sample, hotspot_af=hotspot_af)

        # Append sample somatic variants.
        somatic_vars_df = somatic_vars_df.append(sample_somatic_vars)

    return somatic_vars_df

def get_somatic_vars_by_sample(gemini_db, annotations, hotspot_af=0.0):
    """
    Returns a dataframe for all somatic variants in the database with the following columns:
    sample, chrom, start, ref, alt, gene, cosmic_ids, HP, num_het, mean_af, genotype, allele_freq, alt_depth, depth
    """

    all_vars_df = get_somatic_variants_df(gemini_db, annotations, hotspot_af=hotspot_af)
    add_gt_attrs_cols(all_vars_df)
    var_columns = ["chrom", "start", "ref", "alt", "gene", "cosmic_ids", "HP", "num_het", "mean_af"]

    somatic_vars_cols = ["sample"] + var_columns + ["genotype", "allele_freq", "alt_depth", "depth"]
    somatic_vars_df = pd.DataFrame(columns=somatic_vars_cols)
    for sample in get_samples(gemini_db):
        # Get somatic variants for sample.

        # Somatic variants are those annotated for now.
        anno_cols = [ '%s.%s' % (anno, sample) for anno in annotations]

        # Get counts across all annotations.
        counts = all_vars_df[ all_vars_df[anno_cols] == True ].count(1)

        # Filter for variants in sample and select columns.
        sample_somatic_vars = all_vars_df.loc[counts[counts != 0].index][var_columns + ["gts." + sample, "gt_quals." + sample, "gt_alt_depths." + sample, "gt_depths." + sample]]
        if len(sample_somatic_vars) > 0:
            # Add sample column.
            sample_somatic_vars.insert(0, "sample", pd.Series())
            sample_somatic_vars["sample"] = sample

            # Rename columns to generic names.
            sample_somatic_vars.columns = somatic_vars_cols

            # Append sample somatic variants.
            somatic_vars_df = somatic_vars_df.append(sample_somatic_vars)

    return somatic_vars_df

def clear_het_normal_genotypes(vars_df, samples):
    """
    Clear genotypes and associated information in dataframe for those with genotype REF/.
    """

    def match_fn(df, sample):
        return vars_df[vars_df['gts.' + sample].str.endswith("/.")]

    return clear_genotypes(vars_df, samples, match_fn=match_fn)

def filter_genotypes_in_samples(vars_df, samples, annotations, min_depth=50, min_alt_depth=5, min_anno_af=0.02, min_novel_af=0.1):
    """
    Clear genotypes and associated information in dataframe for those that do not meet criteria.
    """

    def match_fn(df, sample_name):
        # Remove variants that are HET ref or do not meet min depth, alt depth.
        to_remove = df[ ( df['gts.' + sample_name].str.endswith("/.") ) | \
                        ( df['gt_depths.' + sample_name] < min_depth ) | \
                        ( df['gt_alt_depths.' + sample_name] < min_alt_depth ) |
                        ( df['gt_quals.' + sample_name] < min_novel_af ) ]

        # Rescue variants that meet annotation requirements.
        for anno in annotations:
            rescue_vars = to_remove[ ( to_remove[anno] == 1) & \
                                     ( to_remove['gt_quals.' + sample_name] >= min_anno_af) & \
                                     ( df['gts.' + sample_name].str.endswith(("A", "C", "G", "T")) ) & \
                                     ( df['gt_depths.' + sample_name] >= min_depth ) & \
                                     ( df['gt_alt_depths.' + sample_name] >= min_alt_depth ) ]
            # Rescue variants by dropping them from the list of variants to remove.
            to_remove.drop(rescue_vars.index)

        return to_remove

    return clear_genotypes(vars_df, samples, match_fn=match_fn)

def clear_genotypes(vars_df, samples, match_fn=None):
    """
    Clear genotypes and associated information in dataframe for those identified by match function.
    Match function should accept dataframe and sample name return a dataframe of matched variants to clear.
    """

    for sample in samples:
        # Find matching variants.
        cols = ["gt_quals." + sample, "gt_alt_depths." + sample, "gt_depths." + sample]
        index = match_fn(vars_df, sample).index

        # Clear variants for sample.
        vars_df.loc[index, cols] = -1
        vars_df.loc[index, 'gts.' + sample] = './.'

    return vars_df

def reduce_to_somatic(vars_df):
    """
    Reduce variants to those that may be somatic.
    """
    severity = ["MED", "HIGH"]

    somatic_vars = vars_df[(vars_df["impact_severity"].isin(severity)) & (vars_df["HP"] < 5)]

    # Keep SNPs that are in COSMIC. TODO: include variant if in ClinVar.
    somatic_snps = somatic_vars[(somatic_vars["type"] == "snp") & \
                                ( (somatic_vars["cosmic_ids"] != "None") | \
                                  ( (somatic_vars["sift_pred"] == "deleterious") & \
                                    (somatic_vars["polyphen_pred"] == "probably_damaging") ) )]

    somatic_indels = somatic_vars[(somatic_vars["type"] == "indel")]

    return pd.concat([somatic_snps, somatic_indels])


if __name__ == "__main__":
    # Argument setup and parsing.
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", help="Operation to perform")
    parser.add_argument("gemini_db", help="Gemini database to use")
    parser.add_argument("--samples", help="Sample to query for")
    parser.add_argument("--cols", help="Columns to query for")
    parser.add_argument("--gt_count", help="Minimum HET count")
    parser.add_argument("--annotations", help="Annotations to query for")
    parser.add_argument("--output_vcf", help="Write variants to this file")
    parser.add_argument("--header", action="store_true", help="Print header?")
    args = parser.parse_args()
    operation = args.operation
    gemini_db = args.gemini_db

    # Do operation.
    if operation == "find_somatic":
        annotations = args.annotations.split(',')
        samples = args.samples
        t0 = time.time()
        if samples:
            # Get somatic variants in sample.
            print get_somatic_vars_in_sample(gemini_db, annotations, samples, depth=1, hotspot_af=0).to_string()
        else:
            # Get somatic variants in all samples.
            #print get_somatic_vars_by_sample2(gemini_db, annotations, hotspot_af=0).to_string()
            get_genotypes_df(gemini_db, annotations)
        t1 = time.time()
        print "Total time", t1-t0
    elif operation == "compare_replicates":
        print "TODO: connect to function"
    elif operation == "print_samples":
        for sample in get_samples(gemini_db):
            print sample
    elif operation == "query_sample":
        sample = args.sample
        cols = args.cols or "chrom, start, end, ref, alt, gene, cosmic_ids"
        gt_count = args.gt_count or 0
        gt_count = int(gt_count)
        for row in query_sample_het(gemini_db, sample, cols, gt_count):
            print row
    elif operation == "query_samples":
        print add_gt_attrs_cols( get_vars_in_samples(gemini_db, args.samples.split(",")).convert_objects(convert_numeric=True) )