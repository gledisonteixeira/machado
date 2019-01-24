# Copyright 2018 by Embrapa.  All rights reserved.
#
# This code is part of the machado distribution and governed by its
# license. Please see the LICENSE.txt and README.md files that should
# have been included as part of this package for licensing information.

"""Load feature file."""

from machado.models import Cv, Db, Cvterm, Dbxref, Dbxrefprop
from machado.models import Feature, FeatureCvterm, FeatureDbxref, Featureloc
from machado.models import Featureprop, FeatureSynonym
from machado.models import FeatureRelationship, FeatureRelationshipprop
from machado.models import Organism, Pub, PubDbxref, FeaturePub, Synonym
from machado.loaders.common import retrieve_ontology_term, retrieve_organism
from machado.loaders.exceptions import ImportingError
from datetime import datetime, timezone
from django.core.exceptions import ObjectDoesNotExist
from django.db.utils import IntegrityError
from pysam.libctabixproxies import GTFProxy
from time import time
from typing import Dict, List, Set, Union
from urllib.parse import unquote
from Bio.SearchIO._model import Hit

# The following features are handled in a specific manner and should not
# be included in VALID_ATTRS: id, name, and parent
VALID_ATTRS = ['dbxref', 'note', 'display', 'parent', 'alias', 'ontology_term',
               'gene', 'orf_classification', 'ncrna_class', 'pseudo',
               'product', 'is_circular', 'gene_synonym', 'partial']


class FeatureLoader(object):
    """Load feature records."""

    help = 'Load feature records.'

    # initialization of lists/sets to store ignored attributes,
    # ignored goterms, and relationships
    ignored_attrs: Set[str] = set()
    ignored_goterms: Set[str] = set()
    relationships: List[Dict[str, str]] = list()

    def __init__(self,
                 source: str,
                 filename: str,
                 organism: Union[str, Organism],
                 doi: str = None) -> None:
        """Execute the init function."""
        if isinstance(organism, Organism):
            self.organism = organism
        else:
            try:
                self.organism = retrieve_organism(organism)
            except ObjectDoesNotExist as e:
                raise ImportingError(e)

        try:
            self.db, created = Db.objects.get_or_create(name=source.upper())
            self.filename = filename
        except IntegrityError as e:
            raise ImportingError(e)

        self.db_null, created = Db.objects.get_or_create(name='null')
        null_dbxref, created = Dbxref.objects.get_or_create(
            db=self.db_null, accession='null')
        null_cv, created = Cv.objects.get_or_create(name='null')
        null_cvterm, created = Cvterm.objects.get_or_create(
            cv=null_cv,
            name='null',
            definition='',
            dbxref=null_dbxref,
            is_obsolete=0,
            is_relationshiptype=0)
        self.pub, created = Pub.objects.get_or_create(
            miniref='null',
            uniquename='null',
            type_id=null_cvterm.cvterm_id,
            is_obsolete=False)

        self.cvterm_contained_in = retrieve_ontology_term(
            ontology='relationship', term='contained in')
        self.aa_cvterm = retrieve_ontology_term(
            ontology='sequence', term='polypeptide')

        self.so_term_protein_match = retrieve_ontology_term(
                ontology='sequence', term='protein_match')
        # Retrieve DOI's Dbxref
        dbxref_doi = None
        self.pub_dbxref_doi = None
        if doi:
            try:
                dbxref_doi = Dbxref.objects.get(accession=doi)
            except ObjectDoesNotExist as e:
                raise ImportingError(e)
            try:
                self.pub_dbxref_doi = PubDbxref.objects.get(dbxref=dbxref_doi)
            except ObjectDoesNotExist as e:
                raise ImportingError(e)

    def get_attributes(self, attributes: str) -> Dict[str, str]:
        """Get attributes."""
        result = dict()
        fields = attributes.split(";")
        for field in fields:
            key, value = field.split("=")
            result[key.lower()] = unquote(value)
        return result

    def process_attributes(self,
                           feature: object,
                           attrs: Dict[str, str]) -> None:
        """Process the VALID_ATTRS attributes."""
        try:
            cvterm_exact = retrieve_ontology_term('synonym_type', 'exact')
        except ObjectDoesNotExist as e:
            raise ImportingError(e)

        # Don't forget to add the attribute to the constant VALID_ATTRS
        for key in attrs:
            if key not in VALID_ATTRS:
                continue
            elif key in ['ontology_term']:
                # store in featurecvterm
                terms = attrs[key].split(',')
                for term in terms:
                    try:
                        aux_db, aux_term = term.split(':')
                        term_db = Db.objects.get(name=aux_db.upper())
                        dbxref = Dbxref.objects.get(
                            db=term_db, accession=aux_term)
                        cvterm = Cvterm.objects.get(dbxref=dbxref)
                        FeatureCvterm.objects.create(feature=feature,
                                                     cvterm=cvterm,
                                                     pub=self.pub,
                                                     is_not=False,
                                                     rank=0)
                    except ObjectDoesNotExist:
                        self.ignored_goterms.add(term)
            elif key in ['dbxref']:
                dbxrefs = attrs[key].split(',')
                for dbxref in dbxrefs:
                    # It expects just one dbxref formated as XX:012345
                    aux_db, aux_dbxref = dbxref.split(':')
                    db, created = Db.objects.get_or_create(name=aux_db.upper())
                    dbxref, created = Dbxref.objects.get_or_create(
                        db=db, accession=aux_dbxref)
                    FeatureDbxref.objects.create(feature=feature,
                                                 dbxref=dbxref,
                                                 is_current=1)
            elif key in ['alias']:
                synonym, created = Synonym.objects.get_or_create(
                    name=attrs.get(key),
                    defaults={'type_id': cvterm_exact.cvterm_id,
                              'synonym_sgml': attrs.get(key)})
                FeatureSynonym.objects.create(synonym=synonym,
                                              feature=feature,
                                              pub=self.pub,
                                              is_current=True,
                                              is_internal=False)
            else:
                note_dbxref, created = Dbxref.objects.get_or_create(
                    db=self.db_null, accession=key)
                cv_feature_property, created = Cv.objects.get_or_create(
                    name='feature_property')
                note_cvterm, created = Cvterm.objects.get_or_create(
                    cv=cv_feature_property,
                    name=key,
                    dbxref=note_dbxref,
                    defaults={'definition': '',
                              'is_relationshiptype': 0,
                              'is_obsolete': 0})
                featureprop_obj, created = Featureprop.objects.get_or_create(
                    feature=feature, type_id=note_cvterm.cvterm_id, rank=0,
                    defaults={'value': attrs.get(key)})
                if not created:
                    featureprop_obj.value = attrs.get(key)
                    featureprop_obj.save()

    def store_tabix_feature(self, tabix_feature: GTFProxy) -> None:
        """Store tabix feature."""
        attrs = self.get_attributes(tabix_feature.attributes)
        for key in attrs:
            if key not in VALID_ATTRS and key not in ['id', 'name', 'parent']:
                self.ignored_attrs.add(key)

        cvterm = retrieve_ontology_term(ontology='sequence',
                                        term=tabix_feature.feature)

        # set id = auto# for features that lack it
        if attrs.get('id') is None:
            attrs['id'] = 'auto{}'.format(str(time()))

        try:
            dbxref, created = Dbxref.objects.get_or_create(
                db=self.db, accession=attrs['id'])
            Dbxrefprop.objects.get_or_create(
                dbxref=dbxref, type_id=self.cvterm_contained_in.cvterm_id,
                value=self.filename, rank=0)
            feature = Feature.objects.create(
                    organism=self.organism,
                    uniquename=attrs.get('id'),
                    type_id=cvterm.cvterm_id,
                    name=attrs.get('name'),
                    dbxref=dbxref,
                    is_analysis=False,
                    is_obsolete=False,
                    timeaccessioned=datetime.now(timezone.utc),
                    timelastmodified=datetime.now(timezone.utc))
        except IntegrityError as e:
            raise ImportingError(
                    'ID {} already registered. {}'.format(attrs.get('id'), e))

        # DOI: try to link feature to publication's DOI
        if (feature and self.pub_dbxref_doi):
            try:
                FeaturePub.objects.get_or_create(
                        feature=feature,
                        pub_id=self.pub_dbxref_doi.pub_id)
            except IntegrityError as e:
                raise ImportingError(e)

        try:
            srcdb = Db.objects.get(name="FASTA_source")
            srcdbxref = Dbxref.objects.get(accession=tabix_feature.contig,
                                           db=srcdb)
            srcfeature = Feature.objects.get(
                dbxref=srcdbxref, organism=self.organism)
        except ObjectDoesNotExist:
            raise ImportingError(
                "Parent not found: {}. It's required to load "
                "a reference FASTA file before loading features."
                .format(tabix_feature.contig))

        # the database requires -1, 0, and +1 for strand
        if tabix_feature.strand == '+':
            strand = +1
        elif tabix_feature.strand == '-':
            strand = -1
        else:
            strand = 0

        # if row.frame is . phase = None
        # some versions of pysam throws ValueError
        try:
            phase = tabix_feature.frame
            if tabix_feature.frame == '.':
                phase = None
        except ValueError:
            phase = None

        try:
            Featureloc.objects.get_or_create(
                feature=feature,
                srcfeature_id=srcfeature.feature_id,
                fmin=tabix_feature.start,
                is_fmin_partial=False,
                fmax=tabix_feature.end,
                is_fmax_partial=False,
                strand=strand,
                phase=phase,
                locgroup=0,
                rank=0)
        except IntegrityError as e:
            print(feature.uniquename,
                  srcfeature.uniquename,
                  tabix_feature.start,
                  tabix_feature.end,
                  strand,
                  phase)
            raise ImportingError(e)

        self.process_attributes(feature, attrs)

        if attrs.get('parent') is not None:
            self.relationships.append({'object_id': attrs['id'],
                                       'subject_id': attrs['parent']})

        # Additional protrein record for each mRNA with the exact same ID
        if tabix_feature.feature == 'mRNA':
            translation_of = retrieve_ontology_term(ontology='sequence',
                                                    term='translation_of')
            feature_mRNA_translation = Feature.objects.create(
                    organism=self.organism,
                    uniquename=attrs.get('id'),
                    type_id=self.aa_cvterm.cvterm_id,
                    name=attrs.get('name'),
                    dbxref=dbxref,
                    is_analysis=False,
                    is_obsolete=False,
                    timeaccessioned=datetime.now(timezone.utc),
                    timelastmodified=datetime.now(timezone.utc))
            FeatureRelationship.objects.create(object=feature_mRNA_translation,
                                               subject=feature,
                                               type=translation_of,
                                               rank=0)

    def store_relationships(self) -> None:
        """Store the relationships."""
        part_of = retrieve_ontology_term(ontology='sequence',
                                         term='part_of')
        relationships = list()
        features = Feature.objects.exclude(type=self.aa_cvterm)
        for item in self.relationships:
            try:
                # the aa features should be excluded since they were created
                # using the same mRNA ID
                object = features.get(uniquename=item['object_id'],
                                      organism=self.organism)
                subject = features.get(uniquename=item['subject_id'],
                                       organism=self.organism)
                relationships.append(FeatureRelationship(
                    subject_id=subject.feature_id,
                    object_id=object.feature_id,
                    type_id=part_of.cvterm_id,
                    rank=0))
            except ObjectDoesNotExist:
                print('Parent/Feature ({}/{}) not registered.'
                      .format(item['object_id'], item['subject_id']))

        FeatureRelationship.objects.bulk_create(relationships)

    def store_bio_searchio_hit(self, searchio_hit: Hit) -> None:
        """Store tabix feature."""
        if not hasattr(searchio_hit, 'accession'):
            searchio_hit.accession = None
        db, created = Db.objects.get_or_create(
            name=searchio_hit.attributes['Target'].upper())
        dbxref, created = Dbxref.objects.get_or_create(
            db=db, accession=searchio_hit.id)
        feature, created = Feature.objects.get_or_create(
                organism=self.organism,
                uniquename=searchio_hit.id,
                type_id=self.so_term_protein_match.cvterm_id,
                name=searchio_hit.accession,
                dbxref=dbxref,
                defaults={
                    'is_analysis': False,
                    'is_obsolete': False,
                    'timeaccessioned': datetime.now(timezone.utc),
                    'timelastmodified': datetime.now(timezone.utc)})
        if not created:
            return None

        for aux_dbxref in searchio_hit.dbxrefs:
            aux_db, aux_term = aux_dbxref.split(':')
            if aux_db == 'GO':
                try:
                    term_db = Db.objects.get(name=aux_db.upper())
                    dbxref = Dbxref.objects.get(
                        db=term_db, accession=aux_term)
                    cvterm = Cvterm.objects.get(dbxref=dbxref)
                    FeatureCvterm.objects.get_or_create(
                        feature=feature, cvterm=cvterm, pub=self.pub,
                        is_not=False, rank=0)
                except ObjectDoesNotExist:
                    self.ignored_goterms.add(aux_dbxref)
            else:
                term_db, created = Db.objects.get_or_create(
                    name=aux_db.upper())
                dbxref, created = Dbxref.objects.get_or_create(
                    db=term_db, accession=aux_term)
                FeatureDbxref.objects.get_or_create(
                    feature=feature, dbxref=dbxref, is_current=1)

        return None

    def store_feature_annotation(self,
                                 feature: str,
                                 cvterm: str,
                                 annotation: str) -> None:
        """Store feature annotation."""
        attrs = {cvterm: annotation}
        for key in attrs:
            if key not in VALID_ATTRS:
                self.ignored_attrs.add(key)

        features = Feature.objects.filter(name=feature)

        if len(features) == 0:
            raise ImportingError('{} not found.'.format(feature))

        for feature_obj in features:
            self.process_attributes(feature_obj, attrs)

    def store_feature_relationships_group(
                               self,
                               group: list,
                               term:  Union[str, Cvterm],
                               value: str = None,
                               ontology: Union[str, Cv] = 'relationship',
                               ) -> None:
        """Store Feature Relationship."""
        # check if retrieving cvterm is needed
        if isinstance(term, Cvterm):
            cvterm = term
        else:
            if isinstance(ontology, Cv):
                cv = ontology
            else:
                try:
                    cv = Cv.objects.get(name=ontology)
                except ObjectDoesNotExist as e:
                    raise ImportingError(e)
            try:
                cvterm = retrieve_ontology_term(
                        ontology=cv, term=term)
            except IntegrityError as e:
                raise ImportingError(e)
        for member in group:
            tempgroup = group.copy()
            tempgroup.remove(member)
            for othermember in tempgroup:
                try:
                    feature_relationship = FeatureRelationship.objects.create(
                                            subject_id=member.feature_id,
                                            object_id=othermember.feature_id,
                                            type_id=cvterm.cvterm_id,
                                            value=value,
                                            rank=0)
                except IntegrityError as e:
                    raise ImportingError(e)
                try:
                    FeatureRelationshipprop.objects.create(
                                feature_relationship=feature_relationship,
                                type_id=self.cvterm_contained_in.cvterm_id,
                                value=self.filename,
                                rank=0)
                except IntegrityError as e:
                    raise ImportingError(e)
