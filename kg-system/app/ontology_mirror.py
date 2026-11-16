from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from zipfile import ZipFile

from rdflib import Graph, Literal, RDF, RDFS, OWL, SKOS, URIRef
from rdflib.plugins.parsers.ntriples import W3CNTriplesParser
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

SKOSXL_PREF = URIRef("http://www.w3.org/2004/02/skos/core-xl#prefLabel")
SKOSXL_ALT = URIRef("http://www.w3.org/2004/02/skos/core-xl#altLabel")
SKOSXL_HIDDEN = URIRef("http://www.w3.org/2004/02/skos/core-xl#hiddenLabel")
SKOSXL_LITERAL = URIRef("http://www.w3.org/2004/02/skos/core-xl#literalForm")
IAO_DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")
OBO_SYNONYMS = {
    URIRef("http://www.geneontology.org/formats/oboInOwl#hasExactSynonym"): "alternative",
    URIRef("http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym"): "alternative",
    URIRef("http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym"): "alternative",
    URIRef("http://www.geneontology.org/formats/oboInOwl#hasNarrowSynonym"): "alternative",
}
LABEL_PREDICATES = {
    RDFS.label: "preferred",
    SKOS.prefLabel: "preferred",
    SKOS.altLabel: "alternative",
    SKOS.hiddenLabel: "hidden",
    **OBO_SYNONYMS,
}
DEFINITION_PREDICATES = {SKOS.definition, IAO_DEFINITION, RDFS.comment}
RELATION_PREDICATES = {
    RDFS.subClassOf: "is_a",
    SKOS.broader: "broader",
    SKOS.narrower: "narrower",
    SKOS.related: "related",
    OWL.equivalentClass: "equivalent",
    URIRef("http://purl.obolibrary.org/obo/BFO_0000050"): "part_of",
}

CONCEPT_PROPERTY_DDL = """
CREATE TABLE IF NOT EXISTS "ConceptProperty" (
  "propertyKey" TEXT PRIMARY KEY,
  "snapshotId" TEXT NOT NULL,
  "externalId" TEXT NOT NULL,
  "externalUri" TEXT NOT NULL,
  "propertyType" TEXT NOT NULL CHECK ("propertyType" IN ('object','data','annotation')),
  "domainKey" TEXT,
  "rangeKey" TEXT,
  "rangeDatatype" TEXT,
  "labelZh" TEXT,
  "labelEn" TEXT,
  "createdAt" TEXT NOT NULL,
  "updatedAt" TEXT NOT NULL,
  UNIQUE ("snapshotId", "externalId"),
  CHECK (NOT ("rangeKey" IS NOT NULL AND "rangeDatatype" IS NOT NULL)),
  FOREIGN KEY ("snapshotId") REFERENCES "OntologySnapshot"("snapshotId") ON DELETE CASCADE,
  FOREIGN KEY ("snapshotId", "domainKey") REFERENCES "OntologyConcept"("snapshotId", "conceptKey") ON DELETE CASCADE,
  FOREIGN KEY ("snapshotId", "rangeKey") REFERENCES "OntologyConcept"("snapshotId", "conceptKey") ON DELETE CASCADE
)
"""

MIRROR_DDL = [
    """
    CREATE TABLE IF NOT EXISTS "OntologyScheme" (
      "schemeId" TEXT PRIMARY KEY,
      "vocab" TEXT NOT NULL CHECK ("vocab" IN ('FOODON','AGROVOC','DFC','KGAP')),
      "schemeName" TEXT NOT NULL,
      "namespace" TEXT NOT NULL,
      "version" TEXT NOT NULL,
      "releasedAt" TEXT NOT NULL,
      "license" TEXT NOT NULL,
      "attribution" TEXT NOT NULL,
      "status" TEXT NOT NULL CHECK ("status" IN ('有效','停用')),
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("vocab", "version")
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "OntologySnapshot" (
      "snapshotId" TEXT PRIMARY KEY,
      "schemeId" TEXT NOT NULL,
      "packageHash" TEXT NOT NULL,
      "packageUri" TEXT NOT NULL,
      "fetchedAt" TEXT NOT NULL,
      "conceptCount" INTEGER NOT NULL CHECK ("conceptCount" >= 0),
      "labelCount" INTEGER NOT NULL CHECK ("labelCount" >= 0),
      "relationCount" INTEGER NOT NULL CHECK ("relationCount" >= 0),
      "propertyCount" INTEGER NOT NULL CHECK ("propertyCount" >= 0),
      "failedCount" INTEGER NOT NULL DEFAULT 0 CHECK ("failedCount" >= 0),
      "failedDetail" TEXT,
      "status" TEXT NOT NULL CHECK ("status" IN ('抽取中','已校验','已发布','失败')),
      "note" TEXT,
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("schemeId", "packageHash"),
      FOREIGN KEY ("schemeId") REFERENCES "OntologyScheme"("schemeId") ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "OntologyConcept" (
      "conceptKey" TEXT PRIMARY KEY,
      "snapshotId" TEXT NOT NULL,
      "externalId" TEXT NOT NULL,
      "externalUri" TEXT NOT NULL,
      "kind" TEXT NOT NULL CHECK ("kind" IN ('class','concept')),
      "localName" TEXT,
      "depth" INTEGER CHECK ("depth" IS NULL OR "depth" >= 0),
      "status" TEXT NOT NULL CHECK ("status" IN ('有效','停用')),
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("snapshotId", "externalId"),
      UNIQUE ("snapshotId", "conceptKey"),
      FOREIGN KEY ("snapshotId") REFERENCES "OntologySnapshot"("snapshotId") ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "ConceptLabel" (
      "labelId" TEXT PRIMARY KEY,
      "conceptKey" TEXT NOT NULL,
      "lang" TEXT NOT NULL CHECK ("lang" IN ('zh','en')),
      "label" TEXT NOT NULL CHECK (length(trim("label")) > 0),
      "labelType" TEXT NOT NULL CHECK ("labelType" IN ('preferred','alternative','hidden')),
      "source" TEXT,
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("conceptKey", "lang", "labelType", "label"),
      FOREIGN KEY ("conceptKey") REFERENCES "OntologyConcept"("conceptKey") ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "ConceptDefinition" (
      "defId" TEXT PRIMARY KEY,
      "conceptKey" TEXT NOT NULL,
      "lang" TEXT NOT NULL CHECK ("lang" IN ('zh','en')),
      "definition" TEXT NOT NULL CHECK (length(trim("definition")) > 0),
      "source" TEXT,
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("conceptKey", "lang"),
      FOREIGN KEY ("conceptKey") REFERENCES "OntologyConcept"("conceptKey") ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "ConceptRelation" (
      "relationId" TEXT PRIMARY KEY,
      "snapshotId" TEXT NOT NULL,
      "fromKey" TEXT NOT NULL,
      "toKey" TEXT NOT NULL,
      "relationType" TEXT NOT NULL CHECK ("relationType" IN ('is_a','broader','narrower','related','part_of','equivalent')),
      "source" TEXT,
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      UNIQUE ("snapshotId", "fromKey", "toKey", "relationType"),
      FOREIGN KEY ("snapshotId") REFERENCES "OntologySnapshot"("snapshotId") ON DELETE CASCADE,
      FOREIGN KEY ("snapshotId", "fromKey") REFERENCES "OntologyConcept"("snapshotId", "conceptKey") ON DELETE CASCADE,
      FOREIGN KEY ("snapshotId", "toKey") REFERENCES "OntologyConcept"("snapshotId", "conceptKey") ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "ConceptMapping" (
      "mappingId" TEXT PRIMARY KEY,
      "externalVocab" TEXT NOT NULL CHECK ("externalVocab" IN ('FOODON','AGROVOC','DFC','KGAP')),
      "externalId" TEXT NOT NULL,
      "externalLabel" TEXT NOT NULL,
      "externalVersion" TEXT,
      "localType" TEXT NOT NULL CHECK ("localType" IN ('Concept','Commodity','CommodityForm','Product','Term','Concept_Synonym','Event','Enterprise','MarketPlace','TimePeriod','IndicatorSeries','UnitOfMeasure','PriceTransmissionLink','ImpactLink','TransformationProcess')),
      "localId" TEXT NOT NULL,
      "matchType" TEXT NOT NULL CHECK ("matchType" IN ('exactMatch','closeMatch','broadMatch','narrowMatch','relatedMatch','structural')),
      "confidence" TEXT NOT NULL CHECK ("confidence" IN ('高','中','低')),
      "reviewedBy" TEXT,
      "reviewedAt" TEXT,
      "status" TEXT NOT NULL CHECK ("status" IN ('待建','已建','已复核','停用')),
      "note" TEXT,
      "createdAt" TEXT NOT NULL,
      "updatedAt" TEXT NOT NULL,
      CHECK ("status" <> '已复核' OR ("reviewedBy" IS NOT NULL AND "reviewedAt" IS NOT NULL))
    )
    """,
]

MIRROR_INDEXES = [
    'CREATE INDEX IF NOT EXISTS "idx_ontologyconcept_externalid" ON "OntologyConcept"("externalId")',
    'CREATE INDEX IF NOT EXISTS "idx_ontologyconcept_snapshot_kind" ON "OntologyConcept"("snapshotId", "kind")',
    'CREATE INDEX IF NOT EXISTS "idx_conceptlabel_concept_lang" ON "ConceptLabel"("conceptKey", "lang")',
    'CREATE INDEX IF NOT EXISTS "idx_conceptlabel_label" ON "ConceptLabel"("label")',
    'CREATE INDEX IF NOT EXISTS "idx_conceptrelation_from_type" ON "ConceptRelation"("fromKey", "relationType")',
    'CREATE INDEX IF NOT EXISTS "idx_conceptrelation_to_type" ON "ConceptRelation"("toKey", "relationType")',
    'CREATE INDEX IF NOT EXISTS "idx_conceptmapping_local" ON "ConceptMapping"("localType", "localId")',
    'CREATE UNIQUE INDEX IF NOT EXISTS "ux_conceptmapping_active_external" ON "ConceptMapping"("externalVocab", "externalId") WHERE "status" <> \'停用\'',
    'CREATE UNIQUE INDEX IF NOT EXISTS "ux_conceptmapping_active_record" ON "ConceptMapping"("externalVocab", "externalId", "localType", "localId") WHERE "status" <> \'停用\'',
]


@dataclass
class ExtractionResult:
    vocab: str
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    labels: list[dict[str, Any]] = field(default_factory=list)
    definitions: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, str]] = field(default_factory=list)
    properties: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def add_label(self, concept_uri: str, literal: Literal, label_type: str) -> None:
        lang = normalize_language(literal.language)
        if lang is None:
            if not literal.language and str(literal).strip():
                self.failures.append({
                    "externalId": external_id(concept_uri, self.vocab),
                    "stage": "label-language",
                    "reason": "untagged literal excluded; zh/en cannot be confirmed",
                })
            return
        text = str(literal).strip()
        if text:
            self.labels.append({"conceptUri": concept_uri, "lang": lang, "label": text, "labelType": label_type})

    def add_definition(self, concept_uri: str, literal: Literal) -> None:
        lang = normalize_language(literal.language)
        if lang is None:
            if not literal.language and str(literal).strip():
                self.failures.append({
                    "externalId": external_id(concept_uri, self.vocab),
                    "stage": "definition-language",
                    "reason": "untagged literal excluded; zh/en cannot be confirmed",
                })
            return
        text = str(literal).strip()
        if text:
            self.definitions.append({"conceptUri": concept_uri, "lang": lang, "definition": text})

    def finalize(self) -> "ExtractionResult":
        concept_uris = set(self.concepts)
        self.labels = _unique_dicts(
            (row for row in self.labels if row["conceptUri"] in concept_uris),
            ("conceptUri", "lang", "labelType", "label"),
        )
        self.definitions = _unique_dicts(
            (row for row in self.definitions if row["conceptUri"] in concept_uris),
            ("conceptUri", "lang", "definition"),
        )
        relations: dict[tuple[str, str, str], dict[str, str]] = {}
        dangling: list[dict[str, Any]] = []
        for row in self.relations:
            if row["fromUri"] in concept_uris and row["toUri"] in concept_uris:
                key = (row["fromUri"], row["toUri"], row["relationType"])
                relations[key] = row
            else:
                dangling.append({
                    "externalId": external_id(row["fromUri"], self.vocab),
                    "stage": "relation-target",
                    "reason": "relation endpoint is not a declared class/concept in the selected T-Box",
                    "toUri": row["toUri"],
                    "relationType": row["relationType"],
                })
        self.relations = [relations[key] for key in sorted(relations)]
        self.failures.extend(dangling)
        self.failures = _unique_dicts(
            self.failures,
            ("externalId", "stage", "reason", "toUri", "relationType"),
        )
        self.properties.sort(key=lambda row: row["externalUri"])
        self._set_depths()
        return self

    def _set_depths(self) -> None:
        parents: dict[str, set[str]] = defaultdict(set)
        children: dict[str, set[str]] = defaultdict(set)
        for row in self.relations:
            if row["relationType"] in {"is_a", "broader"}:
                parents[row["fromUri"]].add(row["toUri"])
                children[row["toUri"]].add(row["fromUri"])
        queue = deque()
        for uri in self.concepts:
            self.concepts[uri]["depth"] = None
            if not parents[uri]:
                self.concepts[uri]["depth"] = 0
                queue.append(uri)
        while queue:
            parent = queue.popleft()
            depth = self.concepts[parent]["depth"]
            for child in children[parent]:
                if self.concepts[child]["depth"] is None:
                    self.concepts[child]["depth"] = depth + 1
                    queue.append(child)


def _unique_dicts(rows: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    unique = {tuple(row.get(key) for key in keys): row for row in rows}
    return [unique[key] for key in sorted(unique, key=lambda item: tuple("" if value is None else str(value) for value in item))]


def normalize_language(language: str | None) -> str | None:
    if not language:
        return None
    base = language.lower().split("-", 1)[0]
    return base if base in {"zh", "en"} else None


def local_name(uri: str) -> str:
    return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def external_id(uri: str, vocab: str) -> str:
    return local_name(uri) if vocab == "AGROVOC" else uri


def initialize_mirror_schema(conn: Connection) -> None:
    for ddl in MIRROR_DDL[:3]:
        conn.exec_driver_sql(ddl)
    for ddl in MIRROR_DDL[3:6]:
        conn.exec_driver_sql(ddl)
    row = conn.exec_driver_sql('SELECT sql FROM sqlite_master WHERE type = \'table\' AND name = \'ConceptProperty\'').fetchone()
    if row and ('"labelEn"' not in row[0] or "'annotation'" not in row[0]):
        conn.exec_driver_sql('ALTER TABLE "ConceptProperty" RENAME TO "ConceptProperty__legacy"')
        conn.exec_driver_sql(CONCEPT_PROPERTY_DDL)
        old_columns = [item[1] for item in conn.exec_driver_sql('PRAGMA table_info("ConceptProperty__legacy")').all()]
        target_columns = [
            "propertyKey", "snapshotId", "externalId", "externalUri", "propertyType", "domainKey",
            "rangeKey", "rangeDatatype", "labelZh", "labelEn", "createdAt", "updatedAt",
        ]
        shared = [column for column in target_columns if column in old_columns]
        names = ", ".join(f'"{column}"' for column in shared)
        conn.exec_driver_sql(f'INSERT INTO "ConceptProperty" ({names}) SELECT {names} FROM "ConceptProperty__legacy"')
        conn.exec_driver_sql('DROP TABLE "ConceptProperty__legacy"')
    else:
        conn.exec_driver_sql(CONCEPT_PROPERTY_DDL)
    conn.exec_driver_sql(MIRROR_DDL[6])
    for ddl in MIRROR_INDEXES:
        conn.exec_driver_sql(ddl)


def extract_rdf_graph(vocab: str, graph: Graph) -> ExtractionResult:
    result = ExtractionResult(vocab)
    classes = {uri for uri in graph.subjects(RDF.type, OWL.Class) if isinstance(uri, URIRef)}
    skos_concepts = {uri for uri in graph.subjects(RDF.type, SKOS.Concept) if isinstance(uri, URIRef)}
    for uri in sorted(classes | skos_concepts, key=str):
        deprecated = any(str(value).lower() == "true" for value in graph.objects(uri, OWL.deprecated))
        result.concepts[str(uri)] = {
            "externalId": external_id(str(uri), vocab),
            "externalUri": str(uri),
            "kind": "class" if uri in classes else "concept",
            "localName": local_name(str(uri)),
            "status": "停用" if deprecated else "有效",
        }
    for predicate, label_type in LABEL_PREDICATES.items():
        for subject, literal in graph.subject_objects(predicate):
            if str(subject) in result.concepts and isinstance(literal, Literal):
                result.add_label(str(subject), literal, label_type)
    for predicate in DEFINITION_PREDICATES:
        for subject, literal in graph.subject_objects(predicate):
            if str(subject) in result.concepts and isinstance(literal, Literal):
                result.add_definition(str(subject), literal)
    for predicate, relation_type in RELATION_PREDICATES.items():
        for subject, target in graph.subject_objects(predicate):
            if str(subject) in result.concepts and isinstance(target, URIRef):
                result.relations.append({
                    "fromUri": str(subject), "toUri": str(target), "relationType": relation_type,
                })
    object_properties = {uri for uri in graph.subjects(RDF.type, OWL.ObjectProperty) if isinstance(uri, URIRef)}
    data_properties = {uri for uri in graph.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(uri, URIRef)}
    annotation_properties = {uri for uri in graph.subjects(RDF.type, OWL.AnnotationProperty) if isinstance(uri, URIRef)}
    rdf_properties = {uri for uri in graph.subjects(RDF.type, RDF.Property) if isinstance(uri, URIRef)}
    properties = object_properties | data_properties | annotation_properties | rdf_properties
    for uri in sorted(properties, key=str):
        property_type = "object" if uri in object_properties else "data" if uri in data_properties else "annotation"
        domain_uri = _property_class_endpoint(graph, uri, RDFS.domain, result, required=False)
        range_uri, range_datatype = _property_range(graph, uri, result, property_type)
        labels: dict[str, list[str]] = defaultdict(list)
        for label in graph.objects(uri, RDFS.label):
            if isinstance(label, Literal):
                lang = normalize_language(label.language)
                if lang:
                    labels[lang].append(str(label).strip())
                elif not label.language and str(label).strip():
                    result.failures.append({
                        "externalId": external_id(str(uri), vocab),
                        "stage": "label-language",
                        "reason": "untagged property literal excluded; zh/en cannot be confirmed",
                    })
        result.properties.append({
            "externalId": external_id(str(uri), vocab),
            "externalUri": str(uri),
            "propertyType": property_type,
            "domainUri": domain_uri,
            "rangeUri": range_uri,
            "rangeDatatype": range_datatype,
            "labelZh": sorted(value for value in labels["zh"] if value)[0] if labels["zh"] else None,
            "labelEn": sorted(value for value in labels["en"] if value)[0] if labels["en"] else None,
        })
    return result.finalize()


def _property_class_endpoint(graph: Graph, uri: URIRef, predicate: URIRef, result: ExtractionResult, required: bool) -> str | None:
    values = list(graph.objects(uri, predicate))
    if not values:
        if required:
            result.failures.append({
                "externalId": external_id(str(uri), result.vocab),
                "stage": "property-domain" if predicate == RDFS.domain else "property-range",
                "reason": "not declared in source",
            })
        return None
    if len(values) == 1 and isinstance(values[0], URIRef) and str(values[0]) in result.concepts:
        return str(values[0])
    result.failures.append({
        "externalId": external_id(str(uri), result.vocab),
        "stage": "property-domain" if predicate == RDFS.domain else "property-range",
        "reason": "not a single declared named class",
        "values": sorted(str(value) for value in values),
    })
    return None


def _property_range(graph: Graph, uri: URIRef, result: ExtractionResult, property_type: str) -> tuple[str | None, str | None]:
    values = list(graph.objects(uri, RDFS.range))
    if not values:
        if property_type in {"object", "data"}:
            result.failures.append({
                "externalId": external_id(str(uri), result.vocab),
                "stage": "property-range",
                "reason": "not declared in source",
            })
        return None, None
    if len(values) == 1 and isinstance(values[0], URIRef):
        value = str(values[0])
        if value in result.concepts:
            return value, None
        if property_type in {"data", "annotation"}:
            return None, value
    result.failures.append({
        "externalId": external_id(str(uri), result.vocab),
        "stage": "property-range",
        "reason": "not a single declared named class or datatype",
        "values": sorted(str(value) for value in values),
    })
    return None, None


class _AgrovocSink:
    def __init__(self, result: ExtractionResult):
        self.result = result
        self.xl_links: list[tuple[str, URIRef, str]] = []
        self.xl_literals: dict[str, list[Literal]] = defaultdict(list)
        self.deprecated: set[str] = set()
        self.untagged: list[tuple[str, Literal, str]] = []

    def triple(self, subject: URIRef, predicate: URIRef, obj: Any) -> None:
        if predicate == RDF.type:
            if obj == SKOS.Concept and isinstance(subject, URIRef):
                uri = str(subject)
                self.result.concepts[uri] = {
                    "externalId": local_name(uri), "externalUri": uri, "kind": "concept",
                    "localName": local_name(uri), "status": "有效",
                }
            return
        if predicate in LABEL_PREDICATES and isinstance(obj, Literal):
            if normalize_language(obj.language):
                self.result.add_label(str(subject), obj, LABEL_PREDICATES[predicate])
            elif not obj.language:
                self.untagged.append((str(subject), obj, LABEL_PREDICATES[predicate]))
            return
        if predicate in DEFINITION_PREDICATES and isinstance(obj, Literal):
            if normalize_language(obj.language):
                self.result.add_definition(str(subject), obj)
            elif not obj.language:
                self.untagged.append((str(subject), obj, "definition"))
            return
        if predicate in {SKOSXL_PREF, SKOSXL_ALT, SKOSXL_HIDDEN}:
            self.xl_links.append((str(subject), predicate, str(obj)))
            return
        if predicate == SKOSXL_LITERAL and isinstance(obj, Literal):
            if normalize_language(obj.language) or not obj.language:
                self.xl_literals[str(subject)].append(obj)
            return
        if predicate in {SKOS.broader, SKOS.narrower, SKOS.related} and isinstance(obj, URIRef):
            self.result.relations.append({
                "fromUri": str(subject), "toUri": str(obj), "relationType": RELATION_PREDICATES[predicate],
            })
            return
        if predicate == OWL.deprecated and str(obj).lower() == "true":
            self.deprecated.add(str(subject))


def extract_agrovoc_nt(stream: BinaryIO) -> ExtractionResult:
    result = ExtractionResult("AGROVOC")
    sink = _AgrovocSink(result)
    W3CNTriplesParser(sink).parse(stream)
    xl_types = {SKOSXL_PREF: "preferred", SKOSXL_ALT: "alternative", SKOSXL_HIDDEN: "hidden"}
    for concept_uri, predicate, label_uri in sink.xl_links:
        if concept_uri not in result.concepts:
            continue
        for literal in sink.xl_literals.get(label_uri, []):
            result.add_label(concept_uri, literal, xl_types[predicate])
    for subject, literal, kind in sink.untagged:
        if subject in result.concepts:
            if kind == "definition":
                result.add_definition(subject, literal)
            else:
                result.add_label(subject, literal, kind)
    for uri in sink.deprecated:
        if uri in result.concepts:
            result.concepts[uri]["status"] = "停用"
    return result.finalize()


def _key(prefix: str, snapshot_id: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join((snapshot_id, *parts)).encode("utf-8")).hexdigest()[:24].upper()
    return f"{prefix}-{digest}"


def _executemany(conn: Connection, sql: str, rows: list[tuple[Any, ...]], size: int = 5000) -> None:
    for start in range(0, len(rows), size):
        conn.exec_driver_sql(sql, rows[start:start + size])


def import_extraction(conn: Connection, result: ExtractionResult, metadata: dict[str, Any]) -> dict[str, int]:
    result.finalize()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    counts = {
        "conceptCount": len(result.concepts),
        "labelCount": len(result.labels),
        "relationCount": len(result.relations),
        "propertyCount": len(result.properties),
        "failedCount": len(result.failures),
    }
    scheme = conn.exec_driver_sql(
        'SELECT "version" FROM "OntologyScheme" WHERE "schemeId" = ?', (metadata["schemeId"],)
    ).fetchone()
    if scheme and scheme[0] != metadata["version"]:
        raise ValueError(f"scheme version conflict: {metadata['schemeId']} is {scheme[0]}, not {metadata['version']}")
    conn.exec_driver_sql(
        'INSERT OR IGNORE INTO "OntologyScheme" '
        '("schemeId","vocab","schemeName","namespace","version","releasedAt","license","attribution","status","createdAt","updatedAt") '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (
            metadata["schemeId"], result.vocab, metadata["schemeName"], metadata["namespace"], metadata["version"],
            metadata["releasedAt"], metadata["license"], metadata["attribution"], "有效", now, now,
        ),
    )
    existing = conn.exec_driver_sql(
        'SELECT "snapshotId","conceptCount","labelCount","relationCount","propertyCount","failedCount" '
        'FROM "OntologySnapshot" WHERE "schemeId" = ? AND "packageHash" = ?',
        (metadata["schemeId"], metadata["packageHash"]),
    ).mappings().fetchone()
    if existing:
        stored = {key: existing[key] for key in counts}
        if stored != counts:
            raise ValueError(f"same package replay differs: stored={stored}, extracted={counts}")
        return counts
    snapshot_id = metadata["snapshotId"]
    conn.exec_driver_sql(
        'INSERT INTO "OntologySnapshot" '
        '("snapshotId","schemeId","packageHash","packageUri","fetchedAt","conceptCount","labelCount","relationCount","propertyCount","failedCount","failedDetail","status","note","createdAt","updatedAt") '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (
            snapshot_id, metadata["schemeId"], metadata["packageHash"], metadata["packageUri"], metadata["fetchedAt"],
            counts["conceptCount"], counts["labelCount"], counts["relationCount"], counts["propertyCount"], counts["failedCount"],
            json.dumps(result.failures, ensure_ascii=False, separators=(",", ":")), metadata.get("status", "已校验"),
            metadata.get("note"), now, now,
        ),
    )
    concept_keys = {uri: _key("OCP", snapshot_id, uri) for uri in result.concepts}
    _executemany(conn,
        'INSERT INTO "OntologyConcept" '
        '("conceptKey","snapshotId","externalId","externalUri","kind","localName","depth","status","createdAt","updatedAt") VALUES (?,?,?,?,?,?,?,?,?,?)',
        [(
            concept_keys[uri], snapshot_id, row["externalId"], uri, row["kind"], row["localName"], row["depth"], row["status"], now, now,
        ) for uri, row in sorted(result.concepts.items())],
    )
    _executemany(conn,
        'INSERT INTO "ConceptLabel" ("labelId","conceptKey","lang","label","labelType","source","createdAt","updatedAt") VALUES (?,?,?,?,?,?,?,?)',
        [(
            _key("LBL", snapshot_id, row["conceptUri"], row["lang"], row["labelType"], row["label"]),
            concept_keys[row["conceptUri"]], row["lang"], row["label"], row["labelType"], result.vocab, now, now,
        ) for row in result.labels],
    )
    definitions: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in result.definitions:
        definitions[(row["conceptUri"], row["lang"])].append(row["definition"])
    _executemany(conn,
        'INSERT INTO "ConceptDefinition" ("defId","conceptKey","lang","definition","source","createdAt","updatedAt") VALUES (?,?,?,?,?,?,?)',
        [(
            _key("DEF", snapshot_id, uri, lang), concept_keys[uri], lang, "\n\n".join(sorted(set(values))), result.vocab, now, now,
        ) for (uri, lang), values in sorted(definitions.items())],
    )
    _executemany(conn,
        'INSERT INTO "ConceptRelation" ("relationId","snapshotId","fromKey","toKey","relationType","source","createdAt","updatedAt") VALUES (?,?,?,?,?,?,?,?)',
        [(
            _key("REL", snapshot_id, row["fromUri"], row["toUri"], row["relationType"]), snapshot_id,
            concept_keys[row["fromUri"]], concept_keys[row["toUri"]], row["relationType"], result.vocab, now, now,
        ) for row in result.relations],
    )
    _executemany(conn,
        'INSERT INTO "ConceptProperty" '
        '("propertyKey","snapshotId","externalId","externalUri","propertyType","domainKey","rangeKey","rangeDatatype","labelZh","labelEn","createdAt","updatedAt") '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        [(
            _key("PRP", snapshot_id, row["externalUri"]), snapshot_id, row["externalId"], row["externalUri"], row["propertyType"],
            concept_keys.get(row["domainUri"]), concept_keys.get(row["rangeUri"]), row["rangeDatatype"], row["labelZh"], row["labelEn"], now, now,
        ) for row in result.properties],
    )
    return counts


def validate_snapshot(conn: Connection, snapshot_id: str, expected_concept_count: int | None = None) -> dict[str, Any]:
    snapshot = conn.exec_driver_sql(
        'SELECT s."snapshotId", s."schemeId", s."conceptCount", s."failedCount", s."failedDetail", sc."vocab" '
        'FROM "OntologySnapshot" s JOIN "OntologyScheme" sc ON sc."schemeId" = s."schemeId" WHERE s."snapshotId" = ?',
        (snapshot_id,),
    ).mappings().fetchone()
    if not snapshot:
        raise ValueError(f"snapshot not found: {snapshot_id}")
    actual_concepts = conn.exec_driver_sql(
        'SELECT COUNT(*) FROM "OntologyConcept" WHERE "snapshotId" = ?', (snapshot_id,)
    ).scalar_one()
    expected = actual_concepts if expected_concept_count is None else expected_concept_count
    invalid_languages = conn.exec_driver_sql(
        'SELECT (SELECT COUNT(*) FROM "ConceptLabel" l JOIN "OntologyConcept" c ON c."conceptKey" = l."conceptKey" '
        'WHERE c."snapshotId" = ? AND l.lang NOT IN (\'zh\',\'en\')) + '
        '(SELECT COUNT(*) FROM "ConceptDefinition" d JOIN "OntologyConcept" c ON c."conceptKey" = d."conceptKey" '
        'WHERE c."snapshotId" = ? AND d.lang NOT IN (\'zh\',\'en\'))',
        (snapshot_id, snapshot_id),
    ).scalar_one()
    duplicate_ids = conn.exec_driver_sql(
        'SELECT COUNT(*) FROM (SELECT "externalId" FROM "OntologyConcept" WHERE "snapshotId" = ? '
        'GROUP BY "externalId" HAVING COUNT(*) > 1)', (snapshot_id,)
    ).scalar_one()
    isolated = conn.exec_driver_sql(
        'SELECT COUNT(*) FROM "OntologyConcept" c WHERE c."snapshotId" = ? AND NOT EXISTS ('
        'SELECT 1 FROM "ConceptRelation" r WHERE r."snapshotId" = c."snapshotId" '
        'AND r."relationType" IN (\'is_a\',\'broader\',\'narrower\') '
        'AND (r."fromKey" = c."conceptKey" OR r."toKey" = c."conceptKey"))',
        (snapshot_id,),
    ).scalar_one()
    dangling = conn.exec_driver_sql(
        'SELECT COUNT(*) FROM "ConceptRelation" r WHERE r."snapshotId" = ? AND ('
        'NOT EXISTS (SELECT 1 FROM "OntologyConcept" c WHERE c."snapshotId" = r."snapshotId" AND c."conceptKey" = r."fromKey") OR '
        'NOT EXISTS (SELECT 1 FROM "OntologyConcept" c WHERE c."snapshotId" = r."snapshotId" AND c."conceptKey" = r."toKey"))',
        (snapshot_id,),
    ).scalar_one()
    preferred = conn.exec_driver_sql(
        'SELECT COUNT(DISTINCT c."conceptKey") FROM "OntologyConcept" c JOIN "ConceptLabel" l ON l."conceptKey" = c."conceptKey" '
        'WHERE c."snapshotId" = ? AND l."labelType" = \'preferred\' AND l.lang IN (\'zh\',\'en\')',
        (snapshot_id,),
    ).scalar_one()
    coverage = preferred / actual_concepts if actual_concepts else 1.0
    mapping_rows = conn.exec_driver_sql(
        'SELECT "mappingId","externalId","localType","localId" FROM "ConceptMapping" '
        'WHERE "externalVocab" = ? AND "status" IN (\'已建\',\'已复核\')', (snapshot["vocab"],),
    ).mappings().all()
    local_targets = {
        "Concept": ("Concept", "conceptId"),
        "Commodity": ("CmdCommodity", "commodityId"),
        "Product": ("Product", "productId"),
    }
    invalid_mappings: list[str] = []
    pending_mappings: list[str] = []
    for mapping in mapping_rows:
        external_exists = conn.exec_driver_sql(
            'SELECT COUNT(*) FROM "OntologyConcept" WHERE "snapshotId" = ? AND "externalId" = ?',
            (snapshot_id, mapping["externalId"]),
        ).scalar_one()
        target = local_targets.get(mapping["localType"])
        if target is None:
            pending_mappings.append(mapping["mappingId"])
            continue
        table, column = target
        local_exists = conn.exec_driver_sql(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" = ?', (mapping["localId"],)
        ).scalar_one()
        if not external_exists or not local_exists:
            invalid_mappings.append(mapping["mappingId"])
    failed_detail = json.loads(snapshot["failedDetail"] or "[]")
    isolated_ratio = isolated / actual_concepts if actual_concepts else 0.0
    checks = [
        {"checkId": "V-01", "passed": actual_concepts == expected and snapshot["conceptCount"] == expected, "actual": actual_concepts, "expected": expected},
        {"checkId": "V-02", "passed": invalid_languages == 0, "invalidCount": invalid_languages},
        {"checkId": "V-03", "passed": duplicate_ids == 0, "duplicateCount": duplicate_ids},
        {"checkId": "V-04", "passed": None, "reason": "HTTP URI sampling is executed separately"},
        {"checkId": "V-05", "passed": isolated_ratio <= 0.005, "isolatedCount": isolated, "isolatedRatio": round(isolated_ratio, 6)},
        {"checkId": "V-06", "passed": dangling == 0, "danglingCount": dangling},
        {"checkId": "V-07", "passed": coverage >= 0.99, "preferredConcepts": preferred, "coverage": round(coverage, 6)},
        {"checkId": "V-08", "passed": not invalid_mappings and not pending_mappings, "invalidMappings": invalid_mappings, "pendingMappings": pending_mappings},
        {"checkId": "V-09", "passed": None, "reason": "same-package replay is executed separately"},
        {"checkId": "V-10", "passed": len(failed_detail) == snapshot["failedCount"], "failedCount": snapshot["failedCount"], "detailCount": len(failed_detail)},
        {"checkId": "V-11", "passed": None, "reason": "query latency benchmark is executed separately"},
    ]
    core_ids = {"V-01", "V-02", "V-03", "V-05", "V-06", "V-07", "V-08", "V-10"}
    return {
        "snapshotId": snapshot_id,
        "vocab": snapshot["vocab"],
        "checks": checks,
        "corePassed": all(check["passed"] is not False for check in checks if check["checkId"] in core_ids),
    }


SOURCE_FILES = {
    "FOODON": ["FOODON/foodon.owl"],
    "DFC": ["DFC/DFC_FullModel.rdf", "DFC/DFC_BusinessOntology.rdf", "DFC/DFC_TechnicalOntology.rdf", "DFC/DFC_ProductGlossary.rdf", "DFC/facets.rdf"],
    "KGAP": ["KGAP/graph_apto.ttl", "KGAP/alm.ttl"],
}


def _source_metadata(vocab: str, source_dir: Path) -> dict[str, Any]:
    manifest = json.loads((source_dir / "download_manifest.json").read_text(encoding="utf-8"))
    entries = [row for row in manifest["files"] if row["vocab"] == vocab and row["role"] == "tbox"]
    selected_names = SOURCE_FILES.get(vocab, ["AGROVOC/agrovoc_lod.nt.zip"])
    selected = [row for row in entries if row["path"] in selected_names]
    for row in selected:
        path = source_dir / row["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise ValueError(f"source checksum mismatch: {path}")
    package_hash = hashlib.sha256("\n".join(f"{row['path']}:{row['sha256']}" for row in selected).encode("utf-8")).hexdigest()
    code = {"FOODON": "FDN", "AGROVOC": "AGV", "DFC": "DFC", "KGAP": "KGP"}[vocab]
    snapshot_id = f"SNP-{code}-{package_hash[:16].upper()}"
    versions = {
        "FOODON": "v2025-01-29",
        "AGROVOC": "2026-11-05T07:11:40Z",
        "DFC": "v2.0.0+product-v1.8.0+taxonomies-v2.0.0",
        "KGAP": "v1.2+almes-e8eb176f0f48bfb546679d5195b9f730dfbd5ec9",
    }
    names = {
        "FOODON": ("FoodOn", "http://purl.obolibrary.org/obo/", "CC-BY-4.0", "FoodOn Ontology Consortium"),
        "AGROVOC": ("AGROVOC", "http://aims.fao.org/aos/agrovoc/", "CC-BY-4.0", "Food and Agriculture Organization of the United Nations"),
        "DFC": ("Data Food Consortium", "https://w3id.org/dfc/", "AGPL-3.0", "Data Food Consortium"),
        "KGAP": ("C4AI-KGAP APTO / Almes Core", "https://w3id.org/apto/", "CC-BY-NC-4.0; GPL-3.0", "C4AI-KGAP and Almes Core contributors"),
    }
    name, namespace, license_name, attribution = names[vocab]
    released = "2025-01-29T00:00:00Z" if vocab == "FOODON" else "2026-11-05T07:11:40Z" if vocab == "AGROVOC" else "2026-02-10T00:00:00Z" if vocab == "DFC" else "2024-09-10T00:00:00Z"
    return {
        "schemeId": f"SCH-{vocab}-{package_hash[:12].upper()}", "vocab": vocab, "schemeName": name, "namespace": namespace,
        "version": versions[vocab], "releasedAt": released, "license": license_name, "attribution": attribution,
        "snapshotId": snapshot_id, "packageHash": package_hash, "packageUri": selected[0]["url"],
        "fetchedAt": max(row["fetchedAt"] for row in selected),
        "note": "Only explicitly tagged zh/en literals are imported; untagged and unsupported structures are recorded in failedDetail.",
    }


def extract_vocab(vocab: str, source_dir: Path) -> ExtractionResult:
    if vocab == "AGROVOC":
        with ZipFile(source_dir / "AGROVOC/agrovoc_lod.nt.zip") as archive, archive.open("agrovoc_lod.nt") as stream:
            return extract_agrovoc_nt(stream)
    graph = Graph()
    for relative in SOURCE_FILES[vocab]:
        graph.parse(source_dir / relative, format="xml" if relative.endswith((".rdf", ".owl")) else "turtle")
    return extract_rdf_graph(vocab, graph)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import versioned external ontology T-Box mirrors")
    parser.add_argument("--vocab", choices=["FOODON", "AGROVOC", "DFC", "KGAP", "ALL"], default="ALL")
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "grain_oil.db")
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "ontology_sources")
    args = parser.parse_args()
    engine = create_engine(f"sqlite:///{args.database.as_posix()}")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        conn.commit()
        initialize_mirror_schema(conn)
        conn.commit()
    for vocab in ["FOODON", "AGROVOC", "DFC", "KGAP"] if args.vocab == "ALL" else [args.vocab]:
        result = extract_vocab(vocab, args.source_dir)
        metadata = _source_metadata(vocab, args.source_dir)
        with engine.begin() as conn:
            counts = import_extraction(conn, result, metadata)
        print(json.dumps({"vocab": vocab, "snapshotId": metadata["snapshotId"], **counts}, ensure_ascii=False), flush=True)
        del result


if __name__ == "__main__":
    main()
