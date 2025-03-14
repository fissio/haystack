import subprocess
import time
from subprocess import run
from sys import platform
import gc
import uuid
import logging
from pathlib import Path
import responses
from sqlalchemy import create_engine, text

import numpy as np
import psutil
import pytest
import requests

try:
    from elasticsearch import Elasticsearch
    from haystack.document_stores.elasticsearch import ElasticsearchDocumentStore
    from milvus import Milvus
    import weaviate
    
    from haystack.document_stores.weaviate import WeaviateDocumentStore
    from haystack.document_stores.milvus import MilvusDocumentStore
    from haystack.document_stores.graphdb import GraphDBKnowledgeGraph
    from haystack.document_stores.faiss import FAISSDocumentStore
    from haystack.document_stores.sql import SQLDocumentStore

except (ImportError, ModuleNotFoundError) as ie:
    from haystack.utils.import_utils import _optional_component_not_installed
    _optional_component_not_installed('test', "test", ie)

from haystack.document_stores import DeepsetCloudDocumentStore, InMemoryDocumentStore

from haystack.nodes.answer_generator.transformers import Seq2SeqGenerator
    
from haystack.nodes.answer_generator.transformers import RAGenerator, RAGeneratorType
from haystack.modeling.infer import Inferencer, QAInferencer
from haystack.nodes.ranker import SentenceTransformersRanker
from haystack.nodes.document_classifier.transformers import TransformersDocumentClassifier
from haystack.nodes.retriever.sparse import ElasticsearchFilterOnlyRetriever, ElasticsearchRetriever, TfidfRetriever
from haystack.nodes.retriever.dense import DensePassageRetriever, EmbeddingRetriever, TableTextRetriever
from haystack.schema import Document

from haystack.nodes.reader.farm import FARMReader
from haystack.nodes.reader.transformers import TransformersReader
from haystack.nodes.reader.table import TableReader, RCIReader
from haystack.nodes.summarizer.transformers import TransformersSummarizer
from haystack.nodes.translator import TransformersTranslator
from haystack.nodes.question_generator import QuestionGenerator


# To manually run the tests with default PostgreSQL instead of SQLite, switch the lines below
SQL_TYPE = "sqlite"
# SQL_TYPE = "postgres"


SAMPLES_PATH = Path(__file__).parent/"samples"

# to run tests against Deepset Cloud set MOCK_DC to False and set the following params
DC_API_ENDPOINT = "https://DC_API/v1"
DC_TEST_INDEX = "document_retrieval_1"
DC_API_KEY = "NO_KEY"
MOCK_DC = True

def pytest_addoption(parser):
    parser.addoption("--document_store_type", action="store", default="elasticsearch, faiss, memory, milvus, weaviate")


def pytest_generate_tests(metafunc):
    # Get selected docstores from CLI arg
    document_store_type = metafunc.config.option.document_store_type
    selected_doc_stores = [item.strip() for item in document_store_type.split(",")]

    # parametrize document_store fixture if it's in the test function argument list
    # but does not have an explicit parametrize annotation e.g
    # @pytest.mark.parametrize("document_store", ["memory"], indirect=False)
    found_mark_parametrize_document_store = False
    for marker in metafunc.definition.iter_markers('parametrize'):
        if 'document_store' in marker.args[0]:
            found_mark_parametrize_document_store = True
            break
    # for all others that don't have explicit parametrization, we add the ones from the CLI arg
    if 'document_store' in metafunc.fixturenames and not found_mark_parametrize_document_store:
        metafunc.parametrize("document_store", selected_doc_stores, indirect=True)


def _sql_session_rollback(self, attr):
    """
    Inject SQLDocumentStore at runtime to do a session rollback each time it is called. This allows to catch
    errors where an intended operation is still in a transaction, but not committed to the database.
    """
    method = object.__getattribute__(self, attr)
    if callable(method):
        try:
            self.session.rollback()
        except AttributeError:
            pass

    return method


SQLDocumentStore.__getattribute__ = _sql_session_rollback


def pytest_collection_modifyitems(config,items):
    for item in items:

        # add pytest markers for tests that are not explicitly marked but include some keywords
        # in the test name (e.g. test_elasticsearch_client would get the "elasticsearch" marker)
        if "generator" in item.nodeid:
            item.add_marker(pytest.mark.generator)
        elif "summarizer" in item.nodeid:
            item.add_marker(pytest.mark.summarizer)
        elif "tika" in item.nodeid:
            item.add_marker(pytest.mark.tika)
        elif "elasticsearch" in item.nodeid:
            item.add_marker(pytest.mark.elasticsearch)
        elif "graphdb" in item.nodeid:
            item.add_marker(pytest.mark.graphdb)
        elif "pipeline" in item.nodeid:
            item.add_marker(pytest.mark.pipeline)
        elif "slow" in item.nodeid:
            item.add_marker(pytest.mark.slow)
        elif "weaviate" in item.nodeid:
            item.add_marker(pytest.mark.weaviate)

        # if the cli argument "--document_store_type" is used, we want to skip all tests that have markers of other docstores
        # Example: pytest -v test_document_store.py --document_store_type="memory" => skip all tests marked with "elasticsearch"
        document_store_types_to_run = config.getoption("--document_store_type")
        keywords = []
        for i in item.keywords:
            if "-" in i:
                keywords.extend(i.split("-"))
            else:
                keywords.append(i)
        for cur_doc_store in ["elasticsearch", "faiss", "sql", "memory", "milvus", "weaviate"]:
            if cur_doc_store in keywords and cur_doc_store not in document_store_types_to_run:
                skip_docstore = pytest.mark.skip(
                    reason=f'{cur_doc_store} is disabled. Enable via pytest --document_store_type="{cur_doc_store}"')
                item.add_marker(skip_docstore)


@pytest.fixture(scope="function", autouse=True)
def gc_cleanup(request):
    """
    Run garbage collector between tests in order to reduce memory footprint for CI.
    """
    yield
    gc.collect()


@pytest.fixture(scope="session")
def elasticsearch_fixture():
    # test if a ES cluster is already running. If not, download and start an ES instance locally.
    try:
        client = Elasticsearch(hosts=[{"host": "localhost", "port": "9200"}])
        client.info()
    except:
        print("Starting Elasticsearch ...")
        status = subprocess.run(
            ['docker rm haystack_test_elastic'],
            shell=True
        )
        status = subprocess.run(
            ['docker run -d --name haystack_test_elastic -p 9200:9200 -e "discovery.type=single-node" elasticsearch:7.9.2'],
            shell=True
        )
        if status.returncode:
            raise Exception(
                "Failed to launch Elasticsearch. Please check docker container logs.")
        time.sleep(30)


@pytest.fixture(scope="session")
def milvus_fixture():
    # test if a Milvus server is already running. If not, start Milvus docker container locally.
    # Make sure you have given > 6GB memory to docker engine
    try:
        milvus_server = Milvus(uri="tcp://localhost:19530", timeout=5, wait_timeout=5)
        milvus_server.server_status(timeout=5)
    except:
        print("Starting Milvus ...")
        status = subprocess.run(['docker run -d --name milvus_cpu_0.10.5 -p 19530:19530 -p 19121:19121 '
                                 'milvusdb/milvus:0.10.5-cpu-d010621-4eda95'], shell=True)
        time.sleep(40)

@pytest.fixture(scope="session")
def weaviate_fixture():
    # test if a Weaviate server is already running. If not, start Weaviate docker container locally.
    # Make sure you have given > 6GB memory to docker engine
    try:
        weaviate_server = weaviate.Client(url='http://localhost:8080', timeout_config=(5, 15))
        weaviate_server.is_ready()
    except:
        print("Starting Weaviate servers ...")
        status = subprocess.run(
            ['docker rm haystack_test_weaviate'],
            shell=True
        )
        status = subprocess.run(
            ['docker run -d --name haystack_test_weaviate -p 8080:8080 semitechnologies/weaviate:1.7.2'],
            shell=True
        )
        if status.returncode:
            raise Exception(
                "Failed to launch Weaviate. Please check docker container logs.")
        time.sleep(60)

@pytest.fixture(scope="session")
def graphdb_fixture():
    # test if a GraphDB instance is already running. If not, download and start a GraphDB instance locally.
    try:
        kg = GraphDBKnowledgeGraph()
        # fail if not running GraphDB
        kg.delete_index()
    except:
        print("Starting GraphDB ...")
        status = subprocess.run(
            ['docker rm haystack_test_graphdb'],
            shell=True
        )
        status = subprocess.run(
            ['docker run -d -p 7200:7200 --name haystack_test_graphdb docker-registry.ontotext.com/graphdb-free:9.4.1-adoptopenjdk11'],
            shell=True
        )
        if status.returncode:
            raise Exception(
                "Failed to launch GraphDB. Please check docker container logs.")
        time.sleep(30)


@pytest.fixture(scope="session")
def tika_fixture():
    try:
        tika_url = "http://localhost:9998/tika"
        ping = requests.get(tika_url)
        if ping.status_code != 200:
            raise Exception(
                "Unable to connect Tika. Please check tika endpoint {0}.".format(tika_url))
    except:
        print("Starting Tika ...")
        status = subprocess.run(
            ['docker run -d --name tika -p 9998:9998 apache/tika:1.24.1'],
            shell=True
        )
        if status.returncode:
            raise Exception(
                "Failed to launch Tika. Please check docker container logs.")
        time.sleep(30)


@pytest.fixture(scope="session")
def xpdf_fixture():
    verify_installation = run(["pdftotext"], shell=True)
    if verify_installation.returncode == 127:
        if platform.startswith("linux"):
            platform_id = "linux"
            sudo_prefix = "sudo"
        elif platform.startswith("darwin"):
            platform_id = "mac"
            # For Mac, generally sudo need password in interactive console.
            # But most of the cases current user already have permission to copy to /user/local/bin.
            # Hence removing sudo requirement for Mac.
            sudo_prefix = ""
        else:
            raise Exception(
                """Currently auto installation of pdftotext is not supported on {0} platform """.format(platform)
            )
        commands = """ wget --no-check-certificate https://dl.xpdfreader.com/xpdf-tools-{0}-4.03.tar.gz &&
                       tar -xvf xpdf-tools-{0}-4.03.tar.gz &&
                       {1} cp xpdf-tools-{0}-4.03/bin64/pdftotext /usr/local/bin""".format(platform_id, sudo_prefix)
        run([commands], shell=True)

        verify_installation = run(["pdftotext -v"], shell=True)
        if verify_installation.returncode == 127:
            raise Exception(
                """pdftotext is not installed. It is part of xpdf or poppler-utils software suite.
                 You can download for your OS from here: https://www.xpdfreader.com/download.html."""
            )


@pytest.fixture(scope="function")
def deepset_cloud_fixture():    
    if MOCK_DC:
        responses.add(
            method=responses.GET, 
            url=f"{DC_API_ENDPOINT}/workspaces/default/indexes/{DC_TEST_INDEX}",
            match=[responses.matchers.header_matcher({"authorization": f"Bearer {DC_API_KEY}"})],
            json={
                    "indexing": 
                        {
                            "status": "INDEXED",
                            "pending_file_count": 0,
                            "total_file_count": 31
                        }
                }, 
            status=200)
    else:
        responses.add_passthru(DC_API_ENDPOINT)


@pytest.fixture(scope="function")
@responses.activate
def deepset_cloud_document_store(deepset_cloud_fixture):
    return DeepsetCloudDocumentStore(api_endpoint=DC_API_ENDPOINT, api_key=DC_API_KEY, index=DC_TEST_INDEX)


@pytest.fixture(scope="function")
def rag_generator():
    return RAGenerator(
        model_name_or_path="facebook/rag-token-nq",
        generator_type=RAGeneratorType.TOKEN,
        max_length=20
    )


@pytest.fixture(scope="function")
def question_generator():
    return QuestionGenerator(model_name_or_path="valhalla/t5-small-e2e-qg")


@pytest.fixture(scope="function")
def eli5_generator():
    return Seq2SeqGenerator(model_name_or_path="yjernite/bart_eli5", max_length=20)


@pytest.fixture(scope="function")
def summarizer():
    return TransformersSummarizer(
        model_name_or_path="google/pegasus-xsum",
        use_gpu=-1
    )


@pytest.fixture(scope="function")
def en_to_de_translator():
    return TransformersTranslator(
        model_name_or_path="Helsinki-NLP/opus-mt-en-de",
    )


@pytest.fixture(scope="function")
def de_to_en_translator():
    return TransformersTranslator(
        model_name_or_path="Helsinki-NLP/opus-mt-de-en",
    )


@pytest.fixture(scope="function")
def test_docs_xs():
    return [
        # current "dict" format for a document
        {"content": "My name is Carla and I live in Berlin", "meta": {"meta_field": "test1", "name": "filename1"}},
        # metafield at the top level for backward compatibility
        {"content": "My name is Paul and I live in New York", "meta_field": "test2", "name": "filename2"},
        # Document object for a doc
        Document(content="My name is Christelle and I live in Paris", meta={"meta_field": "test3", "name": "filename3"})
    ]


@pytest.fixture(scope="function")
def reader_without_normalized_scores():
    return FARMReader(
        model_name_or_path="distilbert-base-uncased-distilled-squad",
        use_gpu=False,
        top_k_per_sample=5,
        num_processes=0,
        use_confidence_scores=False
    )


@pytest.fixture(params=["farm", "transformers"], scope="function")
def reader(request):
    if request.param == "farm":
        return FARMReader(
            model_name_or_path="distilbert-base-uncased-distilled-squad",
            use_gpu=False,
            top_k_per_sample=5,
            num_processes=0
        )
    if request.param == "transformers":
        return TransformersReader(
            model_name_or_path="distilbert-base-uncased-distilled-squad",
            tokenizer="distilbert-base-uncased",
            use_gpu=-1
        )


@pytest.fixture(params=["tapas", "rci"], scope="function")
def table_reader(request):
    if request.param == "tapas":
        return TableReader(model_name_or_path="google/tapas-base-finetuned-wtq")
    elif request.param == "rci":
        return RCIReader(row_model_name_or_path="michaelrglass/albert-base-rci-wikisql-row",
                         column_model_name_or_path="michaelrglass/albert-base-rci-wikisql-col")


@pytest.fixture(scope="function")
def ranker_two_logits():
    return SentenceTransformersRanker(
        model_name_or_path="deepset/gbert-base-germandpr-reranking",
    )

@pytest.fixture(scope="function")
def ranker():
    return SentenceTransformersRanker(
        model_name_or_path="cross-encoder/ms-marco-MiniLM-L-12-v2",
    )


@pytest.fixture(scope="function")
def document_classifier():
    return TransformersDocumentClassifier(
        model_name_or_path="bhadresh-savani/distilbert-base-uncased-emotion",
        use_gpu=False
    )

@pytest.fixture(scope="function")
def zero_shot_document_classifier():
    return TransformersDocumentClassifier(
        model_name_or_path="cross-encoder/nli-distilroberta-base",
        use_gpu=False,
        task="zero-shot-classification",
        labels=["negative", "positive"]
    )

@pytest.fixture(scope="function")
def batched_document_classifier():
    return TransformersDocumentClassifier(
        model_name_or_path="bhadresh-savani/distilbert-base-uncased-emotion",
        use_gpu=False,
        batch_size=16
    )

@pytest.fixture(scope="function")
def indexing_document_classifier():
    return TransformersDocumentClassifier(
        model_name_or_path="bhadresh-savani/distilbert-base-uncased-emotion",
        use_gpu=False,
        batch_size=16,
        classification_field="class_field"
    )

# TODO Fix bug in test_no_answer_output when using
# @pytest.fixture(params=["farm", "transformers"])
@pytest.fixture(params=["farm"], scope="function")
def no_answer_reader(request):
    if request.param == "farm":
        return FARMReader(
            model_name_or_path="deepset/roberta-base-squad2",
            use_gpu=False,
            top_k_per_sample=5,
            no_ans_boost=0,
            return_no_answer=True,
            num_processes=0
        )
    if request.param == "transformers":
        return TransformersReader(
            model_name_or_path="deepset/roberta-base-squad2",
            tokenizer="deepset/roberta-base-squad2",
            use_gpu=-1,
            top_k_per_candidate=5
        )


@pytest.fixture(scope="function")
def prediction(reader, test_docs_xs):
    docs = [Document.from_dict(d) if isinstance(d, dict) else d for d in test_docs_xs]
    prediction = reader.predict(query="Who lives in Berlin?", documents=docs, top_k=5)
    return prediction


@pytest.fixture(scope="function")
def no_answer_prediction(no_answer_reader, test_docs_xs):
    docs = [Document.from_dict(d) if isinstance(d, dict) else d for d in test_docs_xs]
    prediction = no_answer_reader.predict(query="What is the meaning of life?", documents=docs, top_k=5)
    return prediction


@pytest.fixture(params=["es_filter_only", "elasticsearch", "dpr", "embedding", "tfidf", "table_text_retriever"])
def retriever(request, document_store):
    return get_retriever(request.param, document_store)


# @pytest.fixture(params=["es_filter_only", "elasticsearch", "dpr", "embedding", "tfidf"])
@pytest.fixture(params=["tfidf"])
def retriever_with_docs(request, document_store_with_docs):
    return get_retriever(request.param, document_store_with_docs)


def get_retriever(retriever_type, document_store):

    if retriever_type == "dpr":
        retriever = DensePassageRetriever(document_store=document_store,
                                          query_embedding_model="facebook/dpr-question_encoder-single-nq-base",
                                          passage_embedding_model="facebook/dpr-ctx_encoder-single-nq-base",
                                          use_gpu=False, embed_title=True)
    elif retriever_type == "tfidf":
        retriever = TfidfRetriever(document_store=document_store)
        retriever.fit()
    elif retriever_type == "embedding":
        retriever = EmbeddingRetriever(
            document_store=document_store,
            embedding_model="deepset/sentence_bert",
            use_gpu=False
        )
    elif retriever_type == "retribert":
        retriever = EmbeddingRetriever(document_store=document_store,
                                       embedding_model="yjernite/retribert-base-uncased",
                                       model_format="retribert",
                                       use_gpu=False)
    elif retriever_type == "elasticsearch":
        retriever = ElasticsearchRetriever(document_store=document_store)
    elif retriever_type == "es_filter_only":
        retriever = ElasticsearchFilterOnlyRetriever(document_store=document_store)
    elif retriever_type == "table_text_retriever":
        retriever = TableTextRetriever(document_store=document_store,
                                       query_embedding_model="deepset/bert-small-mm_retrieval-question_encoder",
                                       passage_embedding_model="deepset/bert-small-mm_retrieval-passage_encoder",
                                       table_embedding_model="deepset/bert-small-mm_retrieval-table_encoder",
                                       use_gpu=False)
    else:
        raise Exception(f"No retriever fixture for '{retriever_type}'")

    return retriever


def ensure_ids_are_correct_uuids(docs:list,document_store:object)->None:
    # Weaviate currently only supports UUIDs
    if type(document_store)==WeaviateDocumentStore:
        for d in docs:
            d["id"] = str(uuid.uuid4())


@pytest.fixture(params=["elasticsearch", "faiss", "memory", "milvus", "weaviate"])
def document_store_with_docs(request, test_docs_xs, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(768))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], tmp_path=tmp_path)
    document_store.write_documents(test_docs_xs)
    yield document_store
    document_store.delete_documents()

@pytest.fixture
def document_store(request, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(768))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], tmp_path=tmp_path)
    yield document_store
    document_store.delete_documents()

@pytest.fixture(params=["memory", "faiss", "milvus", "elasticsearch"])
def document_store_dot_product(request, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(768))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], similarity="dot_product", tmp_path=tmp_path)
    yield document_store
    document_store.delete_documents()

@pytest.fixture(params=["memory", "faiss", "milvus", "elasticsearch"])
def document_store_dot_product_with_docs(request, test_docs_xs, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(768))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], similarity="dot_product", tmp_path=tmp_path)
    document_store.write_documents(test_docs_xs)
    yield document_store
    document_store.delete_documents()

@pytest.fixture(params=["elasticsearch", "faiss", "memory", "milvus"])
def document_store_dot_product_small(request, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(3))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], similarity="dot_product", tmp_path=tmp_path)
    yield document_store
    document_store.delete_documents()

@pytest.fixture(params=["elasticsearch", "faiss", "memory", "milvus", "weaviate"])
def document_store_small(request, tmp_path):
    embedding_dim = request.node.get_closest_marker("embedding_dim", pytest.mark.embedding_dim(3))
    document_store = get_document_store(document_store_type=request.param, embedding_dim=embedding_dim.args[0], similarity="cosine", tmp_path=tmp_path)
    yield document_store
    document_store.delete_documents()


@pytest.fixture(scope="function", autouse=True)
def postgres_fixture():
    if SQL_TYPE == "postgres":
        setup_postgres()
        yield
        teardown_postgres()
    else:
        yield


@pytest.fixture
def sql_url(tmp_path):
    return  get_sql_url(tmp_path)


def get_sql_url(tmp_path):
    if SQL_TYPE == "postgres":
        return "postgresql://postgres:postgres@127.0.0.1/postgres"
    else:
        return f"sqlite:///{tmp_path}/haystack_test.db"


def setup_postgres():
    # status = subprocess.run(["docker run --name postgres_test -d -e POSTGRES_HOST_AUTH_METHOD=trust -p 5432:5432 postgres"], shell=True)
    # if status.returncode:
    #     logging.warning("Tried to start PostgreSQL through Docker but this failed. It is likely that there is already an existing instance running.")
    # else:
    #     sleep(5)
    engine = create_engine('postgresql://postgres:postgres@127.0.0.1/postgres', isolation_level='AUTOCOMMIT')

    with engine.connect() as connection:
        try:
            connection.execute(text('DROP SCHEMA public CASCADE'))
        except Exception as e:
            logging.error(e)
        connection.execute(text('CREATE SCHEMA public;'))
        connection.execute(text('SET SESSION idle_in_transaction_session_timeout = "1s";'))

        
def teardown_postgres():
    engine = create_engine('postgresql://postgres:postgres@127.0.0.1/postgres', isolation_level='AUTOCOMMIT')
    with engine.connect() as connection:
        connection.execute(text('DROP SCHEMA public CASCADE'))
        connection.close()


def get_document_store(document_store_type, tmp_path, embedding_dim=768, embedding_field="embedding", index="haystack_test", similarity:str="cosine"): # cosine is default similarity as dot product is not supported by Weaviate
    if document_store_type == "sql":
        document_store = SQLDocumentStore(url=get_sql_url(tmp_path), index=index, isolation_level="AUTOCOMMIT")

    elif document_store_type == "memory":
        document_store = InMemoryDocumentStore(
            return_embedding=True, embedding_dim=embedding_dim, embedding_field=embedding_field, index=index, similarity=similarity)
        
    elif document_store_type == "elasticsearch":
        # make sure we start from a fresh index
        client = Elasticsearch()
        client.indices.delete(index=index+'*', ignore=[404])
        document_store = ElasticsearchDocumentStore(
            index=index, return_embedding=True, embedding_dim=embedding_dim, embedding_field=embedding_field, similarity=similarity
        )

    elif document_store_type == "faiss":
        document_store = FAISSDocumentStore(
            embedding_dim=embedding_dim,
            sql_url=get_sql_url(tmp_path),
            return_embedding=True,
            embedding_field=embedding_field,
            index=index,
            similarity=similarity,
            isolation_level="AUTOCOMMIT"
        )

    elif document_store_type == "milvus":
        document_store = MilvusDocumentStore(
            embedding_dim=embedding_dim,
            sql_url=get_sql_url(tmp_path),
            return_embedding=True,
            embedding_field=embedding_field,
            index=index,
            similarity=similarity,
            isolation_level="AUTOCOMMIT"
        )
        _, collections = document_store.milvus_server.list_collections()
        for collection in collections:
            if collection.startswith(index):
                document_store.milvus_server.drop_collection(collection)
    
    elif document_store_type == "weaviate":
        document_store = WeaviateDocumentStore(
            weaviate_url="http://localhost:8080",
            index=index,
            similarity=similarity,
            embedding_dim=embedding_dim,
        )
        document_store.weaviate_client.schema.delete_all()
        document_store._create_schema_and_index_if_not_exist()
    else:
        raise Exception(f"No document store fixture for '{document_store_type}'")

    return document_store


@pytest.fixture(scope="function")
def adaptive_model_qa(num_processes):
    """
    PyTest Fixture for a Question Answering Inferencer based on PyTorch.
    """
    try:
        model = Inferencer.load(
            "deepset/bert-base-cased-squad2",
            task_type="question_answering",
            batch_size=16,
            num_processes=num_processes,
            gpu=False,
        )
        yield model
    finally:
        if num_processes != 0:
            # close the pool
            # we pass join=True to wait for all sub processes to close
            # this is because below we want to test if all sub-processes
            # have exited
            model.close_multiprocessing_pool(join=True)

    # check if all workers (sub processes) are closed
    current_process = psutil.Process()
    children = current_process.children()
    assert len(children) == 0


@pytest.fixture(scope="function")
def bert_base_squad2(request):
    model = QAInferencer.load(
            "deepset/minilm-uncased-squad2",
            task_type="question_answering",
            batch_size=4,
            num_processes=0,
            multithreading_rust=False,
            use_fast=True # TODO parametrize this to test slow as well
    )
    return model



DOCS_WITH_EMBEDDINGS = [
    Document(
        content="""The capital of Germany is the city state of Berlin.""",
        embedding=np.array([2.22920075e-01, 1.07770450e-02, 3.35382462e-01, -7.27265477e-02,
                           -1.98119566e-01, -5.64537346e-02, 6.09261453e-01, 2.87229061e-01,
                           -7.73971230e-02, -2.23876238e-01, -5.47461927e-01, -1.08676875e+00,
                           2.95721531e-01, 7.53905892e-01, -3.36153835e-01, 1.94666490e-01,
                           2.92297024e-02, 6.56022906e-01, 2.67616689e-01, -3.81376356e-01,
                           -2.98582464e-01, -1.89207539e-01, 6.07246757e-01, 1.67709842e-01,
                           2.75577039e-01, -9.33986664e-01, 4.31648612e-01, -1.00929722e-01,
                           -4.82133955e-01, 7.30958655e-02, -4.85000134e-01, -1.17192902e-01,
                           -2.78178096e-01, 6.61195964e-02, 4.15457308e-01, 3.25128995e-02,
                           2.66546309e-01, 1.30013347e-01, 3.52349013e-01, -6.64731681e-01,
                           -6.83372736e-01, -3.16153020e-01, 3.67267191e-01, -4.05127078e-01,
                           -8.20419341e-02, -1.00207639e+00, -2.10523933e-01, 9.38237131e-01,
                           -2.96095699e-01, -1.82708800e-01, -9.05334055e-01, 2.68770158e-01,
                           3.29131901e-01, 9.00070250e-01, 4.34159547e-01, -5.65743327e-01,
                           -7.94787586e-01, -9.83037204e-02, -1.01550505e-01, 1.17718965e-01,
                           2.48768821e-01, 2.64568210e-01, -1.21708572e-01, 3.54779810e-01,
                           7.25113750e-01, 4.65293467e-01, -4.09185141e-02, -8.67474079e-03,
                           -2.21501254e-02, -6.34054065e-01, -9.91622388e-01, -2.93476105e-01,
                           -3.77548009e-01, -3.20685089e-01, 7.97941908e-02, -4.51179177e-01,
                           1.61721796e-01, 2.01941788e-01, 2.18551666e-01, 8.89380276e-02,
                           9.31400955e-02, -1.68867663e-01, 1.93741471e-01, 9.80174169e-03,
                           3.96567971e-01, 3.25188875e-01, -3.59817073e-02, -3.05083066e-01,
                           -2.91377038e-01, -2.59871781e-01, -2.33116597e-02, 8.39208305e-01,
                           1.92560270e-01, 1.10900663e-01, -6.46018386e-02, -7.44270265e-01,
                           1.63968995e-01, 1.13590971e-01, 2.35207364e-01, 1.82242617e-01,
                           -1.76687598e-01, 1.26516908e-01, 3.98482740e-01, 2.40804136e-01,
                           4.77896258e-02, -6.28400743e-01, -4.66124773e-01, 3.31229940e-02,
                           -2.50761136e-02, 4.35739040e-01, -2.45411038e-01, -2.20042571e-01,
                           8.92485529e-02, -4.65541370e-02, -1.38036475e-01, 5.56459785e-01,
                           5.61165631e-01, -9.59392071e-01, -2.86836233e-02, 2.67911255e-01,
                           4.45386730e-02, 8.50977540e-01, -6.11386299e-02, -1.98372751e-01,
                           4.68791090e-02, -5.06277978e-01, 1.34303793e-01, 7.62167692e-01,
                           -2.64607519e-01, 4.18876261e-02, 2.89180636e-01, 5.62154353e-01,
                           2.11251423e-01, 3.10281783e-01, 7.21961856e-02, -5.72963893e-01,
                           3.83405089e-01, 1.92931354e-01, -4.10556868e-02, 6.54039383e-02,
                           -7.69101679e-01, -3.99726629e-01, 4.27413732e-04, -8.61558840e-02,
                           6.74372464e-02, -6.33262277e-01, 1.29509404e-01, -7.98301876e-01,
                           -1.86452359e-01, -3.74487117e-02, 6.27469346e-02, 5.53238690e-01,
                           -1.18287519e-01, 2.22255215e-01, -3.88892442e-01, -5.07142544e-02,
                           1.17656231e+00, 1.06320940e-01, 4.87917721e-01, 1.30945101e-01,
                           5.19872069e-01, -5.09424150e-01, 7.24166155e-01, 2.54679173e-02,
                           4.71467018e-01, 2.11418241e-01, 7.24739254e-01, 8.81170094e-01,
                           5.08289784e-03, -2.56663375e-02, -7.88815022e-01, -3.99944574e-01,
                           4.35373336e-01, -1.85048744e-01, -3.40764970e-03, -3.34966034e-02,
                           -5.41758597e-01, 1.26321182e-01, 2.60807693e-01, 6.73399121e-03,
                           -2.97145188e-01, 8.47041607e-01, -9.33591202e-02, 5.28455973e-01,
                           -3.99243206e-01, 1.12693250e-01, -1.43983305e-01, 1.67462572e-01,
                           -5.15321195e-02, 4.32413101e-01, 4.54831392e-01, -4.17369545e-01,
                           2.24987328e-01, -7.20562488e-02, 1.14134535e-01, 4.92308468e-01,
                           -8.98665905e-01, 5.71145713e-01, -4.19293523e-01, 3.95240694e-01,
                           1.14924744e-01, -6.05691969e-01, 5.72251439e-01, -2.09341362e-01,
                           -2.33997151e-01, 1.25237420e-01, 1.88679054e-01, -1.65349171e-01,
                           1.09286271e-01, 3.67127098e-02, -8.38237703e-02, -7.06058443e-01,
                           2.02231467e-01, -2.55237550e-01, 1.09513953e-01, -1.31659687e-01,
                           -6.15252674e-01, -3.03829938e-01, 1.37894958e-01, -4.24786448e-01,
                           4.53196496e-01, -1.98051147e-02, -4.47584987e-01, 2.15226576e-01,
                           1.43030539e-01, 1.77718982e-01, -7.88647681e-02, 5.66962123e-01,
                           -4.94479597e-01, -2.12739050e-01, -6.91644847e-03, 3.48478556e-02,
                           -1.28705889e-01, -3.83449569e-02, 5.88934198e-02, -6.58130586e-01,
                           -5.36214471e-01, 2.55122989e-01, 8.75554740e-01, -4.75414962e-01,
                           9.52955902e-01, 1.35785684e-01, 1.23120442e-01, 5.05717754e-01,
                           1.48803160e-01, 4.04200435e-01, 2.76372820e-01, -2.47299239e-01,
                           -8.07143569e-01, 4.83604342e-01, -1.85374618e-01, -1.95024580e-01,
                           -1.25921816e-01, 7.70030096e-02, 1.42136604e-01, -3.08087885e-01,
                           1.59089297e-01, 3.25954586e-01, 1.59884766e-01, 6.17780030e-01,
                           2.01809742e-02, -3.50671440e-01, -6.72550499e-01, 4.13817167e-03,
                           -2.71146059e-01, 7.78259337e-02, -2.49091178e-01, -3.05263430e-01,
                           2.25365594e-01, 3.72759551e-01, 3.85581732e-01, -1.09396994e-01,
                           -3.47314715e-01, -1.12919524e-01, 1.29915655e-01, 8.06079060e-03,
                           3.92060950e-02, 1.97335854e-02, -6.53263927e-01, -1.24800075e-02,
                           -1.32480651e-01, 7.22689390e-01, -2.42579862e-01, -1.25441504e+00,
                           -1.32172257e-02, -1.10918665e+00, 8.78458321e-01, -3.46063733e-01,
                           -3.95923197e-01, 7.50630498e-01, -6.05472811e-02, 3.24168801e-03,
                           -1.87530309e-01, 1.40227765e-01, -2.82838583e-01, 1.33317798e-01,
                           1.89625695e-01, -8.86574462e-02, 2.12340951e-01, -2.99456716e-01,
                           -1.22753668e+00, 3.14115554e-01, -6.14668578e-02, 5.75566962e-02,
                           4.74531233e-01, -4.22935635e-01, -4.82785627e-02, 1.42783090e-01,
                           -5.56570292e+00, -3.73210102e-01, -3.54320705e-01, -4.24514055e-01,
                           -2.42527917e-01, 6.06708586e-01, 1.82795480e-01, 4.26353738e-02,
                           -3.05563331e-01, -5.30404389e-01, 8.72441947e-01, 1.09079361e-01,
                           2.24435870e-02, 6.74964964e-01, 2.53065675e-01, 3.88592303e-01,
                           1.19709507e-01, -3.20065737e-01, -3.31855297e-01, -2.44884327e-01,
                           -3.52100194e-01, -2.81845808e-01, 8.55575204e-01, 1.62181407e-01,
                           7.85247564e-01, -2.96381339e-02, -2.88497955e-01, 6.53568059e-02,
                           -7.08795369e-01, -8.04900110e-01, 4.42439765e-02, -4.29058880e-01,
                           3.26467693e-01, -1.53244287e-01, 3.99080157e-01, 4.89609912e-02,
                           4.12760764e-01, 3.94182920e-01, 1.94052160e-01, -4.93394583e-01,
                           -2.29200646e-02, 4.95266408e-01, -6.72604218e-02, 1.91921741e-02,
                           5.96645474e-01, -2.27546245e-01, -1.71159700e-01, -5.57102934e-02,
                           -8.21766138e-01, 5.46978891e-01, 7.12940097e-02, 2.39723727e-01,
                           3.99156392e-01, -4.60546672e-01, -3.68922651e-01, 4.64805663e-01,
                           9.51609537e-02, 4.60486412e-01, -4.56739873e-01, 1.10822640e-01,
                           -2.06528842e-01, 1.95380542e-02, -1.97392479e-01, -1.14359230e-01,
                           -2.01808512e-02, -1.26127511e-01, -7.95332611e-01, 6.66711032e-01,
                           2.22024679e-01, 7.37181231e-02, -2.25423813e-01, -8.22625279e-01,
                           2.40563720e-01, -6.11137688e-01, -6.11412823e-01, -1.45952356e+00,
                           -1.37649477e-04, -5.29529095e-01, 1.47666246e-01, 3.54295582e-01,
                           -6.83852911e-01, 1.97373703e-01, -1.56224251e-01, -1.39315836e-02,
                           1.52347326e-01, -1.78363323e-01, -2.45118767e-01, 4.62190807e-02,
                           6.59810960e-01, -5.49332023e-01, -3.59251350e-01, -8.36586714e-01,
                           4.37820464e-01, -7.46994615e-01, -6.13222957e-01, 5.13272882e-01,
                           -8.53794441e-02, -5.95119596e-01, 6.63125142e-02, -2.91639060e-01,
                           3.56542349e-01, -2.10935414e-01, -6.86178625e-01, -4.68558609e-01,
                           2.96867311e-01, 1.22527696e-01, 3.36856037e-01, 6.65121257e-01,
                           8.23574543e-01, -4.33361620e-01, -4.60013747e-01, 9.83969793e-02,
                           -2.06140336e-02, -9.26007748e-01, 4.21539873e-01, -1.04092360e+00,
                           -8.31346154e-01, 7.40615308e-01, 2.03596890e-01, -3.54458541e-01,
                           -1.00714087e-01, 6.06378078e-01, -1.25727594e-01, -7.54246935e-02,
                           -4.75891769e-01, -8.33747163e-03, -7.37221539e-01, 8.11691880e-02,
                           -4.76147123e-02, 1.16448052e-01, 6.71404898e-02, 8.09732974e-02,
                           4.06575024e-01, -6.49466589e-02, 6.71445608e-01, 1.64475188e-01,
                           2.04286948e-01, -3.85309786e-01, -1.56549439e-01, -7.14797437e-01,
                           2.02202156e-01, 5.64372897e-01, 3.26015085e-01, 1.32548913e-01,
                           7.73424655e-02, -3.40584129e-01, -1.97809264e-02, 2.23556265e-01,
                           -4.01336968e-01, -6.39470071e-02, 8.09292793e-01, 6.28170550e-01,
                           -6.66837394e-03, -1.85917675e-01, -8.26690435e-01, 2.90101707e-01,
                           3.71746987e-01, 3.63382846e-01, 3.30177546e-01, -1.59507245e-01,
                           4.05810446e-01, -6.38260722e-01, -4.45926607e-01, 3.86468828e-01,
                           5.87992489e-01, -1.67837348e-02, -8.56421649e-01, -1.46168426e-01,
                           1.47883758e-01, -3.02581072e-01, 3.04086179e-01, -3.63289267e-01,
                           -1.37931064e-01, 2.16311902e-01, -2.87937909e-01, 2.17867494e-01,
                           1.85905039e-01, 2.63448834e-01, 3.99643362e-01, 7.09417015e-02,
                           -7.54964426e-02, -5.97936213e-01, -4.81102228e-01, 8.86633337e-01,
                           -2.07774624e-01, 4.67518479e-01, 2.69507207e-02, 3.04756582e-01,
                           1.97698742e-01, 2.01897427e-01, 7.08150089e-01, -1.89206481e-01,
                           -2.48630807e-01, 1.31412342e-01, -3.77624273e-01, -8.65468025e-01,
                           2.81697720e-01, 2.68136449e-02, -3.59579533e-01, -1.67907849e-01,
                           3.39563519e-01, 5.14352739e-01, -5.05458474e-01, 1.13890879e-01,
                           5.56517914e-02, -2.44290620e-01, 5.04581273e-01, 6.74505532e-02,
                           1.21912472e-01, 3.03465098e-01, -1.17531466e+00, -2.73063779e-03,
                           -2.87389159e-01, 5.57524502e-01, -4.76101369e-01, -2.24274755e-01,
                           6.08418345e-01, -8.36228848e-01, -2.73688063e-02, 1.36113301e-01,
                           4.85431179e-02, 3.30218256e-01, 2.45848745e-02, -2.14380369e-01,
                           -1.55451417e-01, 4.74268287e-01, -5.26949689e-02, -3.60365629e-01,
                           1.46594465e-01, -7.30531573e-01, 4.51633155e-01, -5.78917377e-02,
                           -1.14988096e-01, -5.43769002e-02, -5.84075786e-02, -1.25919655e-01,
                           3.54636490e-01, 1.66502092e-02, -2.82013208e-01, 1.53901696e-01,
                           7.34325886e-01, 6.29021525e-01, 1.65552661e-01, -2.53647298e-01,
                           2.58286983e-01, -2.23352492e-01, 1.37091652e-01, -7.11111128e-02,
                           -7.29316473e-01, 3.14306825e-01, -1.94846138e-01, -1.88290656e-01,
                           -5.51873147e-01, -5.86067080e-01, 7.76780128e-01, -3.32203567e-01,
                           1.26009226e-01, 6.82506114e-02, 2.42465541e-01, -4.79260236e-01,
                           -2.64422894e-01, -1.46414991e-02, -1.63076729e-01, 4.91135448e-01,
                           -7.55948648e-02, -4.34874535e-01, -4.70796406e-01, 2.31533051e-01,
                           6.86599091e-02, -4.67249811e-01, -3.49846303e-01, -1.56457052e-01,
                           2.26900756e-01, -3.05055201e-01, 5.14786020e-02, 7.42341399e-01,
                           3.08751971e-01, 3.73778671e-01, -3.72706920e-01, 4.08769280e-01,
                           -4.39344704e-01, 9.28550214e-03, 2.11898744e-01, -8.82063210e-01,
                           2.79842377e-01, -9.61523890e-01, -1.06504209e-01, 4.37725782e-01,
                           -6.85699284e-01, -7.09701329e-02, 2.03018457e-01, -7.74664462e-01,
                           -3.58985692e-01, 2.29817599e-01, -1.63717717e-02, 3.96644741e-01,
                           3.03747296e-01, 7.59450972e-01, 3.05326551e-01, 1.13241032e-01,
                           -3.04896057e-01, 7.16535866e-01, 5.99493623e-01, -3.41223150e-01,
                           8.01769421e-02, 2.74060369e-01, -1.89308211e-01, -7.03582019e-02,
                           6.31491482e-01, -2.66766787e-01, -3.60058993e-02, -4.59694006e-02,
                           4.13341448e-02, 6.20938718e-01, -4.05673683e-01, 3.14955115e-01,
                           8.51010144e-01, -1.57450408e-01, 1.32778615e-01, 3.33602667e-01,
                           -1.68273598e-01, -8.25519502e-01, 3.41632813e-01, 2.60136947e-02,
                           1.49609357e-01, 3.42133522e-01, 3.76624107e-01, -1.45224094e-01,
                           -6.06691055e-02, 1.32190138e-01, 1.17674023e-01, 7.03091204e-01,
                           -5.53888232e-02, 1.80667877e-01, -9.27466691e-01, -2.98746169e-01,
                           1.82669535e-01, 5.05023301e-01, -4.15828198e-01, 4.03008044e-01,
                           2.74655282e-01, 5.33584476e-01, -2.66726196e-01, 3.71005863e-01,
                           6.66988790e-01, -2.88121644e-02, -3.36296678e-01, -4.08127680e-02,
                           2.42406055e-01, -3.95779252e-01, 6.24649882e-01, -2.11524870e-02,
                           -9.01915729e-02, 6.27994537e-04, 2.01389760e-01, -2.37837404e-01,
                           2.33498871e-01, 2.53635406e-01, -1.48279428e-01, -1.29509941e-01,
                           1.57033205e-01, -3.17683131e-01, 9.30176303e-03, 2.08731264e-01,
                           -2.70904571e-01, 1.72909975e-01, -2.84282975e-02, -1.26348920e-02,
                           -5.57500660e-01, -4.00371701e-01, 4.37779486e-01, 2.39513844e-01,
                           -9.83366221e-02, -4.74843085e-01, 7.46401250e-02, 2.65029907e-01,
                           -2.55706310e-01, -4.04609382e-01, 6.11083746e-01, 2.09431499e-01,
                           -4.90902215e-01, -3.79809409e-01, -4.07738805e-01, -8.39593947e-01,
                           -4.45040494e-01, 1.32427901e-01, -2.89512202e-02, -4.05294597e-01,
                           1.10134587e-01, 2.92823054e-02, -7.39422813e-03, -3.82603586e-01,
                           2.00942218e-01, -1.56378612e-01, -6.31098151e-02, -8.38923037e-01,
                           2.18381509e-01, 4.24017191e-01, -1.11921988e-02, -2.06086934e-02,
                           8.83747578e-01, 2.98153669e-01, -1.01949327e-01, 3.51015419e-01,
                           1.73736498e-01, 4.60796833e-01, -1.50092870e-01, -4.06093806e-01,
                           -5.45160949e-01, 8.19314718e-02, 5.16103089e-01, -8.94221127e-01,
                           -2.19182819e-01, 3.07301104e-01, -2.76391618e-02, -1.36747360e-01,
                           3.67395580e-01, -3.47063392e-01, -2.81705800e-02, 1.09726787e-02,
                           2.90009379e-02, -2.60963470e-01, 4.64340061e-01, 7.83061028e-01,
                           4.33903933e-01, 2.94982612e-01, 8.19467902e-01, 6.35938764e-01,
                           -4.21458691e-01, 3.45274419e-01, -7.68046618e-01, -5.41747689e-01,
                           -8.85769799e-02, 1.31434202e-01, -5.25221646e-01, 4.55112815e-01,
                           6.91722155e-01, 6.68683171e-01, -5.47426462e-01, -7.70461857e-01,
                           -1.66969776e-01, 4.41468775e-01, 5.66215217e-01, 3.57928425e-02,
                           2.17262149e-01, -9.86621380e-02, -4.86463368e-01, 2.94643529e-02,
                           -8.25751781e-01, 3.90646994e-01, 3.40370983e-01, 3.16112041e-01,
                           9.32825267e-01, -1.61067862e-02, 2.67292827e-01, -3.85537408e-02,
                           3.50360483e-01, -4.22007769e-01, 8.67691994e-01, 1.73017085e-01],
                           dtype=np.float32)
    ),
    Document(
        content="""Berlin is the capital and largest city of Germany by both area and population.""",
        embedding=np.array([-3.50273997e-02, -2.48432189e-01, 6.22839212e-01, -2.02022746e-01,
                           -3.85405064e-01, 2.25520879e-01, 3.62649381e-01, 5.04554689e-01,
                           -4.23478037e-01, -3.49022627e-01, -3.92042458e-01, -1.29845297e+00,
                           2.67841876e-01, 8.42141330e-01, -1.04595557e-01, 3.08678299e-01,
                           2.04692081e-01, 4.04445529e-01, 1.46137536e-01, 4.34402674e-02,
                           -2.07350522e-01, -1.23709336e-01, 6.03435040e-01, 2.23109469e-01,
                           4.06399101e-01, -8.63346577e-01, 6.71044067e-02, -1.64094806e-01,
                           -2.01718658e-01, 2.26681113e-01, -4.99083489e-01, 3.95566672e-02,
                           -2.56573886e-01, 1.36661112e-01, 4.94901866e-01, -3.79223824e-01,
                           4.39944714e-02, 7.74090067e-02, 1.65991753e-01, -6.05902374e-01,
                           -6.71969056e-01, -3.25198919e-01, 4.57322180e-01, -5.73203489e-02,
                           -1.81668729e-01, -9.46653485e-01, -3.48476291e-01, 7.92435110e-01,
                           -5.79158902e-01, -5.66574708e-02, -8.12117040e-01, 2.75338925e-02,
                           1.85874030e-01, 8.35340858e-01, 4.10750836e-01, -3.47608507e-01,
                           -8.52427721e-01, 2.69759744e-02, -3.20787787e-01, -2.51891077e-01,
                           4.47721303e-01, 3.11386466e-01, -1.98152617e-01, 4.73785162e-01,
                           9.63908017e-01, 1.64340034e-01, 3.54560353e-02, -6.74128532e-03,
                           -8.14039856e-02, -7.24786401e-01, -7.03148484e-01, -2.28851557e-01,
                           -4.46531236e-01, -1.46620423e-01, -2.65437990e-01, -5.92449844e-01,
                           1.21022910e-02, 2.23394483e-03, 1.67981237e-01, -1.21683285e-01,
                           -1.35042474e-01, -3.92371416e-01, 2.45243281e-01, -1.92256197e-01,
                           5.04460752e-01, 2.30800226e-01, -3.78899246e-01, -2.25738496e-01,
                           -4.10815418e-01, -2.89627165e-01, -2.01466121e-02, 6.42084002e-01,
                           3.61558765e-01, 7.81632885e-02, -8.87344405e-02, -5.39395750e-01,
                           1.73859358e-01, 2.29152858e-01, 1.93723273e-02, -1.40379012e-01,
                           -2.77711898e-01, 1.50807753e-01, 4.77448404e-01, 5.50886393e-02,
                           -8.28208998e-02, -6.45287335e-01, -2.45338172e-01, 2.00820148e-01,
                           5.36505699e-01, 1.91126034e-01, -2.88397133e-01, -3.70828032e-01,
                           -7.51306936e-02, -4.33721125e-01, -2.39529923e-01, 2.26737723e-01,
                           3.62281471e-01, -6.49121046e-01, 7.34182149e-02, -1.84938148e-01,
                           -1.40600190e-01, 6.90262318e-01, -4.20865417e-03, 3.37241292e-02,
                           -1.04037970e-01, -5.12658119e-01, 2.85518885e-01, 6.14049435e-01,
                           -3.64280075e-01, -1.79104939e-01, -3.40193689e-01, 6.93493247e-01,
                           1.68136895e-01, 8.19072276e-02, -1.26587659e-01, -2.71052718e-01,
                           -1.73928216e-03, 5.96372664e-01, -1.99492872e-01, -1.85374454e-01,
                           -8.67393851e-01, -3.30342293e-01, -1.45058334e-01, -5.84702380e-03,
                           -1.16527915e-01, -5.52076221e-01, -1.82155043e-01, -9.68754411e-01,
                           1.34406865e-01, -2.16481060e-01, 1.59609109e-01, 7.34762907e-01,
                           -5.40787280e-01, 1.33426115e-01, -1.09678365e-01, -2.30006188e-01,
                           1.11722279e+00, 1.23066463e-01, 5.51491082e-01, 1.47878438e-01,
                           2.54464090e-01, -5.22197366e-01, 6.91881776e-01, 9.90326330e-03,
                           2.30611145e-01, -2.40993947e-02, 8.55854809e-01, 4.04415131e-01,
                           -4.35511768e-02, 2.06272732e-02, -4.83772397e-01, -3.05012941e-01,
                           4.56852138e-01, 1.49446487e-01, -3.67835537e-02, -7.69569874e-02,
                           -6.35296702e-02, 7.86715373e-02, 3.23589087e-01, -3.12235892e-01,
                           -3.00187111e-01, 6.28162324e-01, -1.43585145e-01, 7.48719096e-01,
                           1.29490951e-02, 4.96434420e-02, -2.50086904e-01, 4.04200941e-01,
                           -1.52548060e-01, 3.55123967e-01, 5.69406033e-01, -7.01724470e-01,
                           -4.43677038e-01, -3.60161997e-02, 2.70253092e-01, 2.54021317e-01,
                           -3.79845738e-01, 4.18389171e-01, -3.44766617e-01, 5.34608603e-01,
                           -3.11189979e-01, -5.41499853e-01, 3.03177029e-01, -1.43043652e-01,
                           -3.13276738e-01, 1.96103245e-01, 1.35801420e-01, -2.77429074e-02,
                           2.61924118e-01, 3.11452188e-02, 3.58356237e-02, -8.88519526e-01,
                           -4.75191772e-02, -3.92460853e-01, -8.64207298e-02, -3.05925369e-01,
                           -7.02316165e-01, -1.89588279e-01, 2.78257459e-01, -1.03573985e-01,
                           7.07532704e-01, 1.37647629e-01, -1.70264706e-01, 3.27573344e-02,
                           3.35145622e-01, 2.08075792e-02, -8.76764581e-02, 3.28958869e-01,
                           -2.38072902e-01, -3.96241456e-01, -1.07984692e-01, 1.34444326e-01,
                           -2.46238917e-01, 2.79079139e-01, -1.55260623e-01, -4.74754244e-01,
                           -2.84023583e-01, 1.02182478e-01, 1.05142117e+00, -4.61189121e-01,
                           8.53893757e-01, 1.00932568e-01, 2.80390769e-01, 6.44754589e-01,
                           4.87237096e-01, 3.82993698e-01, 1.41536862e-01, -3.36806178e-01,
                           -5.53201795e-01, 2.69606560e-01, -2.34568954e-01, -2.23571286e-02,
                           -7.33271688e-02, 4.06048372e-02, 5.10511518e-01, -2.35112339e-01,
                           3.62917244e-01, 1.57821834e-01, 4.87915903e-01, 4.96996820e-01,
                           -1.96621269e-01, -2.43224114e-01, -5.20324349e-01, 5.90502322e-02,
                           -2.56272733e-01, 3.61729294e-01, -3.32738519e-01, -5.96960723e-01,
                           2.99745977e-01, 4.65166926e-01, 3.28167796e-01, -1.17259681e-01,
                           -5.01662433e-01, -1.48999184e-01, 2.17762917e-01, -4.10884678e-01,
                           1.44081831e-01, 7.76892304e-02, -4.42914069e-01, 1.29206687e-01,
                           1.71367854e-01, 6.29935980e-01, -2.19932780e-01, -1.30284238e+00,
                           1.45756677e-01, -9.61726546e-01, 6.56495571e-01, -1.52887583e-01,
                           -2.01431572e-01, 2.95827806e-01, -2.80632555e-01, 4.25828323e-02,
                           -2.11607650e-01, -6.34967312e-02, -1.49535969e-01, 1.87844545e-01,
                           2.66154408e-01, 7.77923465e-02, 2.01846510e-01, -4.23298031e-01,
                           -1.10681295e+00, 2.42170855e-01, -6.48393482e-03, -1.13451183e-01,
                           3.46645772e-01, -5.15025079e-01, -1.29404336e-01, 3.50346863e-01,
                           -5.70772457e+00, -8.72777998e-02, -1.83941588e-01, 5.33758998e-02,
                           -2.60958552e-01, 2.95171022e-01, -8.71118158e-03, 1.90805584e-01,
                           -3.13076824e-01, -4.78946745e-01, 5.23603499e-01, 1.21371880e-01,
                           -2.50287764e-02, 7.15912938e-01, 2.29437172e-01, 2.97248602e-01,
                           2.51952589e-01, -1.86277822e-01, 9.31650400e-02, -1.53992958e-02,
                           -4.04607445e-01, -2.78155923e-01, 3.67175877e-01, 3.75201046e-01,
                           4.96505558e-01, 3.66121769e-01, -1.59621641e-01, 1.76051900e-01,
                           -8.60311389e-01, -6.75116122e-01, -1.56537183e-02, -4.97247845e-01,
                           3.06776136e-01, -4.22662735e-01, 6.39765680e-01, 2.48774216e-02,
                           2.31036097e-01, 3.25571090e-01, 9.33806449e-02, -6.65107429e-01,
                           3.17120165e-01, 4.41605687e-01, -1.78470194e-01, -2.38168150e-01,
                           7.48334885e-01, -5.21835506e-01, -3.73043060e-01, 3.62553686e-01,
                           -4.96466815e-01, 2.34892637e-01, 3.01374078e-01, 3.05527598e-01,
                           1.48040339e-01, -2.32049793e-01, -4.06453580e-01, 2.26377442e-01,
                           4.25807416e-01, 4.14450318e-01, -1.77013934e-01, 5.26809469e-02,
                           -2.83185631e-01, -1.47750434e-02, -9.70338732e-02, -4.51183803e-02,
                           5.33358902e-02, -9.44599956e-02, -2.00490251e-01, 3.36551577e-01,
                           1.70389459e-01, 6.69810623e-02, -3.73803884e-01, -3.69580567e-01,
                           2.31907159e-01, -5.82696736e-01, -6.36856616e-01, -8.39038551e-01,
                           -4.10703987e-01, -2.61468112e-01, 2.63036303e-02, 1.72604293e-01,
                           -1.00000954e+00, -1.09753408e-01, -3.69728804e-01, 7.57561699e-02,
                           2.22493976e-01, -2.52135634e-01, -4.21582580e-01, 4.54726070e-02,
                           5.95288694e-01, -1.79186583e-01, 9.71945152e-02, -7.58243561e-01,
                           6.97836161e-01, -5.38700461e-01, -2.39157140e-01, 2.57476777e-01,
                           -1.71200082e-01, -1.90746903e-01, 3.61735761e-01, 6.05483800e-02,
                           6.03016734e-01, -3.64552736e-01, -7.73001552e-01, -4.50461119e-01,
                           3.19612086e-01, 3.50916743e-01, 1.87987700e-01, 1.03144979e+00,
                           9.68851626e-01, -3.40994745e-01, -1.22335069e-01, 1.49947748e-01,
                           -1.03934139e-01, -8.34116578e-01, 6.82787180e-01, -7.01456428e-01,
                           -8.52156222e-01, 9.83630240e-01, 3.32455635e-01, -1.71249673e-01,
                           -3.09638940e-02, 6.74620807e-01, 6.88535199e-02, 1.20285749e-02,
                           -1.89996332e-01, -2.75208242e-02, -6.31824434e-01, 1.28967464e-01,
                           -3.11984301e-01, -5.88281713e-02, 1.64214373e-01, -2.94747557e-02,
                           5.38486123e-01, -1.07168958e-01, 2.51502037e-01, 2.42167205e-01,
                           3.11857104e-01, -5.06378293e-01, -1.26543716e-01, -5.85934401e-01,
                           4.25757676e-01, 7.08761454e-01, 3.90809447e-01, 2.80379802e-01,
                           -1.50237560e-01, 2.49918178e-03, -1.25070959e-01, 2.48671040e-01,
                           -2.42208377e-01, -2.54361063e-01, 6.48561537e-01, 2.21887559e-01,
                           -2.15696931e-01, 1.80307105e-01, -5.25993466e-01, 2.46874169e-01,
                           5.23397386e-01, 5.82857549e-01, 4.52213019e-01, 6.09597191e-02,
                           3.06350380e-01, -9.12604570e-01, -6.82491004e-01, -7.84439147e-02,
                           4.71254766e-01, -3.44983667e-01, -7.07369804e-01, -1.63650617e-01,
                           3.54335994e-01, -1.51956707e-01, 4.20144707e-01, -2.70185262e-01,
                           -2.19435751e-01, 1.45269379e-01, -2.86464304e-01, 2.22090811e-01,
                           -1.77325144e-01, 1.67901158e-01, 2.73082137e-01, 2.36154377e-01,
                           2.24694774e-01, -4.98087257e-01, -4.11527336e-01, 1.09001088e+00,
                           2.04774439e-01, 6.21963859e-01, -9.19686481e-02, 2.32351631e-01,
                           -1.68183297e-01, -9.68001932e-02, 5.58549523e-01, -2.39460319e-01,
                           -3.05309594e-01, 2.14133635e-01, -9.41303298e-02, -8.82585645e-01,
                           3.83934170e-01, 2.19155788e-01, -5.49660802e-01, 1.05475634e-02,
                           -6.32535443e-02, 5.38853288e-01, -6.00962579e-01, 1.27381921e-01,
                           6.89040273e-02, -2.42806375e-01, 6.02003813e-01, 2.98819840e-01,
                           2.75932997e-03, 5.27704470e-02, -1.20524478e+00, -1.19761616e-01,
                           -2.65623242e-01, 6.10629499e-01, -8.19721639e-01, -1.34642273e-02,
                           3.11588407e-01, -8.70028734e-01, -2.76728660e-01, -1.44883553e-02,
                           1.05519965e-02, 4.65373188e-01, -8.50991160e-03, -4.79901612e-01,
                           -5.53666353e-02, 6.45023704e-01, 4.40883525e-02, -3.37444067e-01,
                           1.66669071e-01, -7.71508217e-01, 4.64793175e-01, 1.12663239e-01,
                           7.72295445e-02, 2.11972415e-01, 1.04857258e-01, -2.91724950e-01,
                           3.10113102e-01, -5.58248647e-02, -2.20166266e-01, 2.14233980e-01,
                           7.40465403e-01, 3.50917518e-01, -2.04494998e-01, 3.72638553e-03,
                           2.92335540e-01, 2.86426067e-01, 2.92725444e-01, -1.24314696e-01,
                           -7.32817292e-01, -1.03724509e-01, -6.12943433e-04, -4.29165125e-01,
                           -6.33485794e-01, -4.30804014e-01, 8.27492356e-01, -4.06285793e-01,
                           3.14204842e-01, 2.00469457e-02, 4.55530435e-02, -2.90983647e-01,
                           -2.27689341e-01, -6.81395978e-02, -1.44458458e-01, 6.21065676e-01,
                           -3.66319835e-01, -3.82178009e-01, -6.32210135e-01, 1.07237205e-01,
                           -2.69225568e-01, -4.39629734e-01, -1.83134109e-01, 5.49998134e-02,
                           4.72239286e-01, -3.50412190e-01, -6.82684332e-02, 7.34665513e-01,
                           2.02386290e-01, 1.13228574e-01, -8.60403031e-02, 2.10516557e-01,
                           -3.78268421e-01, 7.76122957e-02, 1.87401593e-01, -7.81153500e-01,
                           -4.67521511e-02, -5.43340206e-01, -2.12380067e-01, 4.36673701e-01,
                           -5.81688881e-01, 5.90436339e-01, 2.56112739e-02, -8.63464177e-01,
                           -2.72117466e-01, 8.26949924e-02, -2.95551158e-02, 2.69366592e-01,
                           1.99876040e-01, 7.31362820e-01, 2.92425871e-01, 4.52556126e-02,
                           -7.45288193e-01, 6.59792423e-01, 1.32268772e-01, -7.25544393e-01,
                           1.63429856e-01, 2.86987305e-01, 2.65547365e-01, -7.67112374e-02,
                           3.05402249e-01, -3.07283700e-02, 2.72607058e-01, 1.11119784e-02,
                           5.46655431e-02, 5.27894378e-01, -5.70523500e-01, 5.23774862e-01,
                           9.90136445e-01, -2.67104745e-01, 2.66145349e-01, 8.45695660e-03,
                           -1.94764182e-01, -8.19335639e-01, 3.73185098e-01, 9.66813508e-03,
                           1.67991698e-01, 3.19511116e-01, 4.94989455e-01, -4.73073393e-01,
                           -1.90819204e-01, 1.00033917e-01, 3.54037255e-01, 3.50040078e-01,
                           -6.15488410e-01, 3.80280077e-01, -6.42622590e-01, -7.59413242e-02,
                           3.32028776e-01, 2.72891670e-01, -7.13512480e-01, 1.67983383e-01,
                           6.72589064e-01, 6.51851475e-01, -4.69424307e-01, 6.19312108e-01,
                           3.20728719e-01, 1.92631543e-01, 3.01389899e-02, 1.28930196e-01,
                           3.21145579e-02, -1.60675317e-01, 5.37545025e-01, -3.68021786e-01,
                           -3.48078310e-01, -2.95386523e-01, 1.20226748e-01, -3.62616450e-01,
                           1.78619117e-01, -1.57189220e-01, 4.32226732e-02, -6.19882271e-02,
                           -1.29041612e-01, -3.06471102e-02, 7.73153007e-02, 2.08616316e-01,
                           -8.33643898e-02, 2.84585238e-01, 2.56437182e-01, -1.31445184e-01,
                           -3.73958707e-01, -1.51229143e-01, 1.24631889e-01, 2.56145447e-01,
                           -1.46403000e-01, -5.62559128e-01, 1.44000500e-01, 7.43318856e-01,
                           -1.76492184e-01, -2.19288468e-03, 4.34121430e-01, 2.52328999e-02,
                           -1.73008680e-01, -3.12634945e-01, -3.77913564e-01, -4.67106253e-01,
                           -5.43051720e-01, -1.42914444e-01, -9.43699777e-02, -5.05417824e-01,
                           -3.18035513e-01, -9.26198438e-02, -1.14961147e-01, -3.74270320e-01,
                           2.06078768e-01, 5.94099090e-02, -1.84328973e-01, -4.85224694e-01,
                           -3.01569700e-03, 4.26463097e-01, -1.51644573e-01, 1.29547372e-01,
                           7.62950063e-01, 1.42216891e-01, -2.64579892e-01, 4.26969469e-01,
                           1.86565630e-02, 6.25447690e-01, -8.33212435e-02, -5.01296699e-01,
                           -4.33987439e-01, 2.94599593e-01, 5.06730258e-01, -4.56841111e-01,
                           -2.52863407e-01, 1.46224990e-01, -1.34221286e-01, 3.59161258e-01,
                           1.44742414e-01, -4.64949697e-01, 9.34459828e-03, -2.75842607e-01,
                           2.41558000e-01, -1.28513649e-01, 1.25265077e-01, 7.52536952e-01,
                           4.12320405e-01, 2.18520522e-01, 7.09124982e-01, 3.20003182e-01,
                           -3.68131578e-01, 3.14773470e-02, -5.25491655e-01, -1.46920130e-01,
                           -5.02151966e-01, 2.66258836e-01, -7.80072629e-01, 6.64352655e-01,
                           9.51846957e-01, 4.84095603e-01, -4.69458699e-01, -6.44100308e-01,
                           -2.09408700e-01, 1.88016608e-01, 5.53907156e-01, 2.46309891e-01,
                           3.69830072e-01, 3.00819635e-01, -5.66207170e-01, 3.10216904e-01,
                           -7.62370825e-01, 6.62293613e-01, 2.95080066e-01, 2.16252878e-01,
                           1.02028084e+00, -5.11510437e-03, 5.37559271e-01, 3.52584869e-01,
                           2.35834837e-01, -4.11791533e-01, 7.64139056e-01, 1.23637088e-01],
                           dtype=np.float32)
    )
]
