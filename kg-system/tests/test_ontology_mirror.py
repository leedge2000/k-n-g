import io
import json
from pathlib import Path

from rdflib import Graph
from sqlalchemy import create_engine, inspect, text

from app.db import Database
from app.ontology_mirror import (
    extract_agrovoc_nt,
    extract_rdf_graph,
    import_extraction,
    initialize_mirror_schema,
    validate_snapshot,
)


RDF_SAMPLE = """
@prefix ex: <https://example.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:Rice a owl:Class ;
    rdfs:label "rice"@en, "水稻"@zh, "riz"@fr, "untagged" ;
    skos:definition "A cereal grain"@en ;
    rdfs:subClassOf ex:Food .
ex:Food a owl:Class ; rdfs:label "food"@en .
ex:hasPart a owl:ObjectProperty ; rdfs:domain ex:Rice .
ex:code a owl:DatatypeProperty ; rdfs:range xsd:string .
ex:note a owl:AnnotationProperty ; rdfs:label "note"@en .
"""


AGROVOC_SAMPLE = """
<http://aims.fao.org/aos/agrovoc/c_1> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2004/02/skos/core#Concept> .
<http://aims.fao.org/aos/agrovoc/c_2> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <http://www.w3.org/2004/02/skos/core#Concept> .
<http://aims.fao.org/aos/agrovoc/c_1> <http://www.w3.org/2004/02/skos/core#broader> <http://aims.fao.org/aos/agrovoc/c_2> .
<http://aims.fao.org/aos/agrovoc/c_1> <http://www.w3.org/2004/02/skos/core-xl#prefLabel> <http://aims.fao.org/aos/agrovoc/xl_en_1> .
<http://aims.fao.org/aos/agrovoc/xl_en_1> <http://www.w3.org/2004/02/skos/core-xl#literalForm> "rice"@en .
<http://aims.fao.org/aos/agrovoc/c_1> <http://www.w3.org/2004/02/skos/core-xl#altLabel> <http://aims.fao.org/aos/agrovoc/xl_zh_1> .
<http://aims.fao.org/aos/agrovoc/xl_zh_1> <http://www.w3.org/2004/02/skos/core-xl#literalForm> "稻谷"@zh .
<http://aims.fao.org/aos/agrovoc/c_1> <http://www.w3.org/2004/02/skos/core-xl#altLabel> <http://aims.fao.org/aos/agrovoc/xl_fr_1> .
<http://aims.fao.org/aos/agrovoc/xl_fr_1> <http://www.w3.org/2004/02/skos/core-xl#literalForm> "riz"@fr .
<http://aims.fao.org/aos/agrovoc/c_2> <http://www.w3.org/2004/02/skos/core#prefLabel> "cereals"@en .
"""


def sample_metadata():
    return {
        "schemeId": "SCH-TEST",
        "vocab": "FOODON",
        "schemeName": "Test ontology",
        "namespace": "https://example.org/",
        "version": "v1",
        "releasedAt": "2026-01-01T00:00:00Z",
        "license": "CC-BY-4.0",
        "attribution": "Test attribution",
        "snapshotId": "SNP-TEST-01",
        "packageHash": "a" * 64,
        "packageUri": "https://example.org/ontology.owl",
        "fetchedAt": "2026-01-01T00:00:00Z",
    }


def test_rdf_extraction_filters_languages_and_keeps_property_without_range():
    graph = Graph().parse(data=RDF_SAMPLE, format="turtle")

    result = extract_rdf_graph("FOODON", graph)

    assert set(result.concepts) == {"https://example.org/Rice", "https://example.org/Food"}
    rice_labels = [row for row in result.labels if row["conceptUri"] == "https://example.org/Rice"]
    assert {(row["lang"], row["label"]) for row in rice_labels} == {("en", "rice"), ("zh", "水稻")}
    assert {row["propertyType"] for row in result.properties} == {"object", "data", "annotation"}
    has_part = next(row for row in result.properties if row["externalUri"] == "https://example.org/hasPart")
    assert has_part["rangeUri"] is None
    assert has_part["rangeDatatype"] is None
    assert any(row["stage"] == "property-range" for row in result.failures)
    assert any(row["stage"] == "label-language" for row in result.failures)
    assert result.relations == [{
        "fromUri": "https://example.org/Rice",
        "toUri": "https://example.org/Food",
        "relationType": "is_a",
    }]


def test_agrovoc_stream_extracts_skos_xl_and_hierarchy():
    result = extract_agrovoc_nt(io.BytesIO(AGROVOC_SAMPLE.encode("utf-8")))

    assert set(result.concepts) == {
        "http://aims.fao.org/aos/agrovoc/c_1",
        "http://aims.fao.org/aos/agrovoc/c_2",
    }
    assert result.concepts["http://aims.fao.org/aos/agrovoc/c_1"]["externalId"] == "c_1"
    assert {(row["lang"], row["label"], row["labelType"]) for row in result.labels} == {
        ("en", "rice", "preferred"),
        ("zh", "稻谷", "alternative"),
        ("en", "cereals", "preferred"),
    }
    assert result.relations == [{
        "fromUri": "http://aims.fao.org/aos/agrovoc/c_1",
        "toUri": "http://aims.fao.org/aos/agrovoc/c_2",
        "relationType": "broader",
    }]


def test_import_is_idempotent_and_has_no_dangling_relations():
    result = extract_rdf_graph("FOODON", Graph().parse(data=RDF_SAMPLE, format="turtle"))
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        initialize_mirror_schema(conn)
        first = import_extraction(conn, result, sample_metadata())
        second = import_extraction(conn, result, sample_metadata())
        counts = {
            table: conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
            for table in ["OntologyConcept", "ConceptLabel", "ConceptRelation", "ConceptProperty"]
        }
        snapshot = conn.execute(text(
            'SELECT "failedCount", "failedDetail" FROM "OntologySnapshot" WHERE "snapshotId" = :id'
        ), {"id": "SNP-TEST-01"}).mappings().one()
        dangling = conn.execute(text(
            'SELECT COUNT(*) FROM "ConceptRelation" r '
            'LEFT JOIN "OntologyConcept" c ON c."snapshotId" = r."snapshotId" AND c."conceptKey" = r."toKey" '
            'WHERE c."conceptKey" IS NULL'
        )).scalar_one()

    assert first == second == {"conceptCount": 2, "labelCount": 3, "relationCount": 1, "propertyCount": 3, "failedCount": 2}
    assert counts == {"OntologyConcept": 2, "ConceptLabel": 3, "ConceptRelation": 1, "ConceptProperty": 3}
    assert snapshot["failedCount"] == len(json.loads(snapshot["failedDetail"]))
    assert dangling == 0


def test_import_rejects_reusing_scheme_id_for_another_version():
    result = extract_rdf_graph("FOODON", Graph().parse(data=RDF_SAMPLE, format="turtle"))
    engine = create_engine("sqlite:///:memory:")
    metadata = sample_metadata()

    with engine.begin() as conn:
        initialize_mirror_schema(conn)
        import_extraction(conn, result, metadata)
        other_version = {**metadata, "version": "v2", "packageHash": "b" * 64, "snapshotId": "SNP-TEST-02"}
        try:
            import_extraction(conn, result, other_version)
        except ValueError as error:
            assert "scheme version conflict" in str(error)
        else:
            raise AssertionError("expected scheme version conflict")


def test_validate_snapshot_checks_core_acceptance():
    result = extract_rdf_graph("FOODON", Graph().parse(data=RDF_SAMPLE, format="turtle"))
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as conn:
        initialize_mirror_schema(conn)
        import_extraction(conn, result, sample_metadata())
        report = validate_snapshot(conn, "SNP-TEST-01", expected_concept_count=2)

    checks = {check["checkId"]: check for check in report["checks"]}
    assert checks["V-01"]["passed"] is True
    assert checks["V-02"]["passed"] is True
    assert checks["V-03"]["passed"] is True
    assert checks["V-05"]["isolatedCount"] == 0
    assert checks["V-06"]["danglingCount"] == 0
    assert checks["V-07"]["coverage"] == 1.0
    assert checks["V-10"]["passed"] is True


def test_database_initialize_creates_mirror_tables():
    seed_dir = Path(__file__).resolve().parents[1] / "seed"
    database = Database("sqlite:///:memory:", seed_dir)

    database.initialize()

    names = set(inspect(database.engine).get_table_names())
    assert {"OntologyScheme", "OntologySnapshot", "OntologyConcept", "ConceptProperty"}.issubset(names)
    columns = {column["name"] for column in inspect(database.engine).get_columns("ConceptProperty")}
    assert "labelEn" in columns


def test_seed_agrovoc_anchors_use_verified_official_concepts():
    seed_dir = Path(__file__).resolve().parents[1] / "seed"
    business = json.loads((seed_dir / "business.json").read_text(encoding="utf-8"))
    concepts = {row["conceptId"]: row for row in business["Concept"]}

    expected = {
        "CON0001": ("AGROVOC-C-6599", "http://aims.fao.org/aos/agrovoc/c_6599"),
        "CON0002": ("AGROVOC-C-8373", "http://aims.fao.org/aos/agrovoc/c_8373"),
        "CON0003": ("AGROVOC-C-12332", "http://aims.fao.org/aos/agrovoc/c_12332"),
        "CON0004": ("AGROVOC-C-14477", "http://aims.fao.org/aos/agrovoc/c_14477"),
    }
    assert {
        concept_id: (concepts[concept_id]["conceptCode"], concepts[concept_id]["externalUri"])
        for concept_id in expected
    } == expected
