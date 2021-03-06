#!/bin/sh

#
# Create a GEMINI database by (a) decomposing and normalizing variants;
# (b) annotating variants with VEP or snpEff; (c) loading annotated variants
# into GEMINI; (d) adding custom annotations to the database.
#
# Dependencies:
#   samtools/bgzip/tabix
#   GNU parallel
#   bcftools
#   VEP and/or snpEff
#   GEMINI
#

# Parameter checking.
if [ $# -lt "2" ]
then
  echo "Usage: `basename $0` <directory of VCFs or single VCF> <db_name> [genome_reference] [VEP/snpEff] [Annotator directory] [custom annos directory]>"
  exit -1
fi

# Set up home directory and default settings.
HOME_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
source ${HOME_DIR}/default_settings.sh

# Set parameters.
INPUT=$1
GEMINI_DB=$2
REFERENCE=${3:-$DEFAULT_REFERENCE}
ANNOTATOR=${4:-$DEFAULT_ANNOTATOR}
ANNOTATOR_DIR=${5:-$DEFAULT_ANNOTATOR_DIR}
CUSTOM_ANNOS_DIR=${6:-$DEFAULT_CUSTOM_ANNOS_DIR}
VEP_FASTA=/Users/jeremy/.vep/homo_sapiens/79_GRCh37/

# If there is a directory of VCFs, combine them into a VCF.
CUR_DIR=${PWD}
if [ -d ${INPUT} ]; then
    # Combine all files in directory into a single file.

    # Go to directory.
    pushd ${INPUT}

    # Compress and index VCFs.
    ${HOME_DIR}/bgzip_and_tabix.sh

    # Merge VCFs into separate records.
    bcftools merge -m none *.vcf.gz > ${CUR_DIR}/all.vcf

    # Input VCF is all VCFs in directory.
    INPUT_VCF=all.vcf

    popd
else
    # Single BCF.
    INPUT_VCF=${INPUT}
fi

#
# HACK specfic to cancer amplicons: replace AF with GQ to get AF (allele frequency) into database as genotype quality.
#
sed -i.bak 's/AF/GQ/g' all.vcf

# Set up name for annotated VCF.
BASE=$(basename "${INPUT_VCF}" .vcf)
ANNO_VCF="${BASE}.anno.vcf"

# Annotate. NOTE: using --refseq, VEP versions 80 and newer do not include GENE symbol.
if [ ${ANNOTATOR} = "VEP" ]; then
    perl ${ANNOTATOR_DIR}/variant_effect_predictor.pl -i ${BASE}.vcf \
    --cache \
    --refseq \
    --offline \
    --assembly GRCh37 \
    --sift b \
    --polyphen b \
    --symbol \
    --numbers \
    --biotype \
    --total_length \
    --hgvs \
    --fasta ${VEP_FASTA} \
    -o ${ANNO_VCF} \
    --vcf \
    --fields Consequence,Codons,Amino_acids,Gene,SYMBOL,Feature,EXON,PolyPhen,SIFT,Protein_position,BIOTYPE,HGVSc,HGVSp
elif [ ${ANNOTATOR} = "snpEff" ]; then
	java -jar ${ANNOTATOR_DIR}/snpEff.jar -i vcf -o vcf GRCh37.75 ${BASE}.vcf > ${ANNO_VCF}
fi

# Load into GEMINI.
gemini load -v ${ANNO_VCF} -t ${ANNOTATOR} ${GEMINI_DB}

#
# HACK specific to cancer amplicons: annotate with HP field from VCF.
#
bgzip ${BASE}.vcf && tabix -p vcf ${BASE}.vcf.gz
gemini annotate -f ${BASE}.vcf.gz -t integer -a extract -c HP -e HP -o first ${GEMINI_DB}

#
# Add annotations to the database.
#
${HOME_DIR}/annotate_gemini_db.sh ${GEMINI_DB} ${CUSTOM_ANNOS_DIR}
