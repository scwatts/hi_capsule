import copy
import logging
import pathlib
import re
import tempfile
import xml.etree.ElementTree as ET


import Bio.Alphabet
import Bio.Seq
import Bio.SeqRecord
import Bio.SeqFeature
import Bio.SeqIO
import Bio.Graphics.GenomeDiagram
import reportlab.lib.colors


from . import database
from . import locus
from . import utility


SEQ_PADDING = 1000

COLOURS_COMPLETE = {'one': '#7bcebe',
                    'two': '#f9958b',
                    'three': '#ffe589',
                    'none': '#d3d3d3',
                   }
COLOURS_BROKEN= {'one': '#8fa8a3',
                  'two': '#d3a7a2',
                  'three': '#d8ceab',
                 }

LABEL_SIZE = 12
TRACK_LABEL_SIZE = 18

HPOINTS_RE = re.compile(r'^([0-9.]+)[^,]+, ([0-9.]+).+$')
VPOINTS_RE = re.compile(r'^.+ ([0-9.]+)$')
PATH_RE = re.compile(r'^M ([0-9.]+).+?L ([0-9.]+).+Z$')
LABEL_RE = re.compile(r'^ matrix\(([-10.]+).+? ?([0-9.]+), ?([0-9.]+)\)')


class SummaryData:

    def __init__(self):
        self.completeness = dict.fromkeys(database.SCHEME, None)
        self.truncated_genes = dict.fromkeys(database.SCHEME, None)
        self.duplicated = None
        self.multiple_contigs = None
        self.serotypes = None
        self.hits_by_contig = None


def create_report(region_groups, nearby_orfs, fasta_fp, prefix, output_dir):
    # Report
    output_report_fp = pathlib.Path(output_dir, '%s.tsv' % prefix)
    summary_data = create_summary(region_groups)
    with output_report_fp.open('w') as fh:
        write_summary(summary_data, fh)

    # Genbank - create
    # Genbank spec requires contig names/ locus names of less than 20 characters but
    # we want full contigs names in the graphic. So we'll truncate the names after
    # generating the graphic
    output_gbk_fp = pathlib.Path(output_dir, '%s.gbk' % prefix)
    genbank_data = create_genbank_record(region_groups, nearby_orfs, fasta_fp)

    # Graphic
    # TODO: legend?
    output_svg_fp = pathlib.Path(output_dir, '%s.svg' % prefix)
    graphic_data = create_graphic(genbank_data, prefix)
    svg_data = patch_graphic(graphic_data)
    with output_svg_fp.open('w') as fh:
        fh.write(svg_data)

    # Genbank - write
    for record in genbank_data:
        record.name = record.name.split()[0][:15]
        record.id = record.id.split()[0][:15]
    with output_gbk_fp.open('w') as fh:
        Bio.SeqIO.write(genbank_data, fh, 'genbank')


def create_summary(region_groups):
    summary_data = SummaryData()
    for region in ('one', 'two', 'three'):
        group = region_groups[region]
        # Completeness and truncated genes
        genes_found = {hit.sseqid for hit in group.hits}
        if region in ('one', 'three'):
            genes_expected = database.SCHEME[region]
        else:
            genes_expected = set()
            for serotype in group.serotypes:
                genes_expected.update(database.SEROTYPES[serotype])
        genes_missing = genes_expected - genes_found
        summary_data.completeness[region] = (genes_missing, len(genes_found), len(genes_expected))
        summary_data.truncated_genes[region] = {hit for hit in group.hits if hit.broken}
        # Duplication. Verbose for clarity
        if group.hits and is_duplicated(group.hits):
            summary_data.duplicated = True

    # Serotype
    summary_data.serotypes = region_groups['two'].serotypes

    # On multiple contigs
    hits_all_gen = (hit for group in region_groups.values() for hit in group.hits)
    contig_hits = locus.sort_hits_by_contig(hits_all_gen)
    if len(contig_hits) > 1:
        summary_data.multiple_contigs = True

    # Store hits sorted by contig
    summary_data.hits_by_contig = contig_hits
    return summary_data


def write_summary(data, fh):
    attributes = list()
    print('#', ','.join(data.serotypes), sep='', file=fh)
    if any(region_complete[0] for region_complete in data.completeness.values()):
        attributes.append('missing_genes')
    else:
        attributes.append('full_gene_complement')
    if any(data.truncated_genes.values()):
        attributes.append('truncated_genes')
    if data.multiple_contigs:
        attributes.append('fragmented_locus')
    if data.duplicated:
        attributes.append('duplicated')
    print('#', end='', file=fh)
    print(*attributes, sep=',', file=fh)
    print('#', end='', file=fh)
    print('contig', 'start', 'end', 'genes', sep='\t', file=fh)
    for contig, contig_hits in data.hits_by_contig.items():
        hits_sorted = sorted(contig_hits, key=lambda k: k.orf.start)
        hits_bounds = [b for hit in hits_sorted for b in (hit.orf.start, hit.orf.end)]
        region_start = min(hits_bounds)
        region_end = max(hits_bounds)
        gene_names = get_gene_names(hits_sorted)
        print(contig, region_start, region_end, ','.join(gene_names), sep='\t', file=fh)


def get_gene_names(hits):
    gene_names = list()
    for hit in hits:
        if hit.broken:
            gene_names.append(hit.sseqid + '*')
        else:
            gene_names.append(hit.sseqid)
    return gene_names


def is_duplicated(hits):
    genes_hits = locus.sort_hits_by_gene(hits)
    gene_counts = dict.fromkeys(genes_hits)
    # Sometimes a single gene is broken into multiple ORFs but wrt to duplication should
    # be considered as a single unit. We do this by setting a bound ~ to expected gene len
    for gene, gene_hits in genes_hits.items():
        hit_first, *hits_sorted = sorted(gene_hits, key=lambda hit: min(hit.orf.start, hit.orf.end))
        upper_bound = min(hit_first.orf.start, hit_first.orf.end) + hit_first.slen * 1.5
        gene_counts[gene] = 1
        for hit in hits_sorted:
            if max(hit.orf.start, hit.orf.end) >= upper_bound:
                gene_counts[gene] += 1
    return any(gene_count > 1 for gene_count in gene_counts.values())


def create_genbank_record(region_groups, nearby_orfs, fasta_fp):
    # TODO: handle sequence boundaries correctly - max(1000 padding or up to the most distant ORF)
    logging.info('Creating genbank records')
    fasta = utility.read_fasta(fasta_fp)
    hits_all = [hit for group in region_groups.values() for hit in group.hits]

    # Create base records
    contigs = {hit.orf.contig for hit in hits_all}
    position_delta = 0
    gb_records = dict()
    for contig in contigs:
        gb_records[contig] = Bio.SeqRecord.SeqRecord(seq='atgc', name=contig, id=fasta_fp.stem)


    # TODO: TEMP
    pd = dict()

    # Add hits
    for contig, contig_hits in locus.sort_hits_by_contig(hits_all).items():
        for hit in sorted(contig_hits, key=lambda k: k.orf.start):
            # TODO TEMP
            position_delta, block_sequence = get_block_sequence(contig_hits, fasta[contig], SEQ_PADDING)
            sequence = Bio.Seq.Seq(block_sequence, Bio.Alphabet.IUPAC.unambiguous_dna)
            gb_records[contig].seq = sequence
            pd[contig] = position_delta

            # Get appropriate representation of gene name
            region = hit.region if hit.region else locus.get_gene_region(hit.sseqid)
            qualifiers = {'gene': hit.sseqid, 'region': region}
            if hit.broken:
                qualifiers['note'] = 'fragment'
            feature_start = hit.orf.start - position_delta if (hit.orf.start - position_delta) > 1 else 1
            feature_end = hit.orf.end - position_delta
            feature_loc = Bio.SeqFeature.FeatureLocation(start=feature_start, end=feature_end, strand=hit.orf.strand)
            feature = Bio.SeqFeature.SeqFeature(location=feature_loc, type='CDS', qualifiers=qualifiers)
            gb_records[contig].features.append(feature)

    # Add ORFs
    orf_counter = 0
    for contig, orfs in locus.sort_orfs_by_contig(nearby_orfs).items():
        # Get current bounds - FeatureLocation.start always smallest regardless of strand
        start = min(gb_records[contig].features, key=lambda k: k.location.start)
        end = max(gb_records[contig].features, key=lambda k: k.location.end)

        # TODO TEMP
        position_delta = pd[contig]

        for orf in sorted(orfs, key=lambda o: o.start):
            orf_counter += 1
            qualifiers = {'gene': 'orf_%s' % orf_counter, 'region': 'none'}
            feature_start = orf.start - position_delta if (orf.start - position_delta) > 1 else 1
            feature_end = orf.end - position_delta
            feature_loc = Bio.SeqFeature.FeatureLocation(start=feature_start, end=feature_end, strand=orf.strand)
            feature = Bio.SeqFeature.SeqFeature(location=feature_loc, type='CDS', qualifiers=qualifiers)
            gb_records[contig].features.append(feature)

    # Add appropriately sizes sequence to records
    for contig, gb_record in gb_records.items():
        #sequence = Bio.Seq.Seq(fasta[contig], Bio.Alphabet.IUPAC.unambiguous_dna)
        pass

    return [record for record in gb_records.values()]


def get_block_sequence(hits, block_contig, margin):
    '''Extract sequence for loci with margin on either side'''
    hits_sorted = sorted(hits, key=lambda k: k.orf.start)
    orf_start = min(hits_sorted[0].orf.start, hits_sorted[0].orf.end)
    orf_end = min(hits_sorted[-1].orf.start, hits_sorted[-1].orf.end)

    # Calculate start end position for sequence with margin
    block_start = orf_start - margin if orf_start >= margin else 0
    block_end = orf_end + margin if (orf_end + margin) <= len(block_contig) else len(block_contig)

    # Return delta in position and sequence with margin
    return block_start, block_contig[block_start:block_end]


def create_graphic(records, prefix):
    graphic = Bio.Graphics.GenomeDiagram.Diagram(prefix)
    for record in records:
        feature_track = graphic.new_track(1, name=record.name, greytrack=True, start=0, end=len(record))
        track_features = feature_track.new_set()
        for feature in record.features:
            if feature.type != 'CDS':
                continue
            # Accept quals as list or single item for interop
            region = get_qualifier(feature.qualifiers['region'])
            if region!= 'none':
                gene_name = get_qualifier(feature.qualifiers['gene'])
                gene_border = reportlab.lib.colors.black
            else:
                gene_name = ''
                gene_border = reportlab.lib.colors.grey
            if 'note' in feature.qualifiers and get_qualifier(feature.qualifiers['note']) == 'fragment':
                gene_colour = COLOURS_BROKEN[region]
            else:
                gene_colour = COLOURS_COMPLETE[region]
            strand = 'start' if feature.strand == 1 else 'end'
            track_features.add_feature(feature, sigil='BIGARROW', label=True, name=gene_name,
                                       border=gene_border, label_position=strand,
                                       label_angle=0, label_size=LABEL_SIZE, color=gene_colour)

        # TODO: Scale width with size
        # TODO: add contig boundaries symbols
        height = len(records) * 150
        end = max(len(record) for record in records)
        graphic.draw(format='linear', pagesize=(1800, height), fragments=1, track_size=0.30, start=0, end=end)
    return graphic


def patch_graphic(graphic_data):
    # TODO: dodge labels for very short contigs
    # TODO: alternatively consider different representation - single block with annotated stop codons
    svg_data = get_svg_data(graphic_data)
    svg_tree = ET.fromstring(svg_data)
    visual_parent = svg_tree.find('.//{http://www.w3.org/2000/svg}g[@transform=""]')

    # Get track bound sizes - must round to 3 significant digits for backwards compatibility
    track_style = 'stroke: rgb(96%,96%,96%); stroke-linecap: butt; stroke-width: 1; fill: rgb(96%,96%,96%);'
    track_backgrounds = svg_tree.findall('.//*[@style="%s"]' % track_style)
    track_hbounds = set()
    for track_background in track_backgrounds:
        bounds = HPOINTS_RE.match(track_background.get('points')).groups()
        bounds = tuple(round(float(bound), 3) for bound in bounds)
        track_hbounds.add(bounds)

    # Send the mid-lines backwards
    # Place them behind the gene symbols but in front track background shading
    paths = svg_tree.findall('.//{http://www.w3.org/2000/svg}g[@transform=""]/{http://www.w3.org/2000/svg}path')
    line_elements = list()
    for path in paths:
        # Round to 3 significant digits for backwards compatibility
        path_hbounds = tuple(round(float(bound), 3) for bound in PATH_RE.match(path.get('d')).groups())
        if path_hbounds in track_hbounds:
            # Remove mid lines and store for later insert at appropriate position
            line_elements.append(path)
            visual_parent.remove(path)
        elif path_hbounds[0] not in {p for hbounds in track_hbounds for p in hbounds}:
            # Remove x-axis ticks
            visual_parent.remove(path)

    # Insert track midpoint lines immediately after track background elements
    for line_element in line_elements:
        visual_parent.insert(len(track_hbounds), line_element)

    # Fix reversed, mirrored labels and pad other labels
    # Genes on the non-coding strand have their labels upside down which is difficult to read
    # Set the padding to be equal
    transform_template = ' matrix(1.000000, 0.000000, -0.000000, 1.000000, %s, %s)'
    texts = svg_tree.findall('.//{http://www.w3.org/2000/svg}g[@transform=""]/{http://www.w3.org/2000/svg}g')
    for text in texts:
        a, x, y = LABEL_RE.match(text.get('transform')).groups()
        if float(a) == -1:
            y_adjusted = float(y) - LABEL_SIZE
            transform = transform_template % (x, y_adjusted)
            text.set('transform', transform)
        else:
            y_adjusted = float(y) + 3
            transform = transform_template % (x, y_adjusted)
            text.set('transform', transform)

    # Remove original track name label - on short tracks, these don't even appear
    name_style = 'font-family: Helvetica; font-size: 8px; fill: rgb(60%,60%,60%);'
    names = svg_tree.findall('.//*[@style="%s"]..' % name_style)
    for name in names:
        visual_parent.remove(name)

    # Add contig names as track labels
    contig_names = [track.name for track in graphic_data.tracks.values()]
    track_vbounds = [VPOINTS_RE.match(tb.get('points')).group(1) for tb in track_backgrounds]

    # Get the correct position to insert these labels
    for insert_index, element in enumerate(visual_parent):
        if element.tag == '{http://www.w3.org/2000/svg}g':
            break

    # Create new elements and place them into the svg document
    text_format = 'font-family: Helvetica; font-size: %spx; fill: rgb(0%%,0%%,0%%);' % TRACK_LABEL_SIZE
    text_transform = 'translate(0,0) scale(1,-1)'
    text_attribs = {'style': text_format, 'transform': text_transform, 'x': '0', 'y': '0'}
    text_element = ET.Element('{http://www.w3.org/2000/svg}text', attrib=text_attribs)
    for contig_name, vbound, hbounds in zip(contig_names, track_vbounds, track_hbounds):
        x = hbounds[0]
        y = float(vbound) + TRACK_LABEL_SIZE
        name_group = ET.Element('{http://www.w3.org/2000/svg}g', attrib={'transform': transform_template % (x, y)})
        name_text = copy.deepcopy(text_element)
        name_text.text = contig_name
        name_group.append(name_text)
        visual_parent.insert(insert_index, name_group)

    return ET.tostring(svg_tree, encoding='unicode')


def get_svg_data(graphic_data):
    # Writing to string fails under Python3, must write to disk
    with tempfile.TemporaryFile('w+') as fh:
        graphic_data.write(fh, 'SVG')
        fh.seek(0)
        return fh.read()


def get_qualifier(qualifier):
    return qualifier[0] if isinstance(qualifier, list) else qualifier
