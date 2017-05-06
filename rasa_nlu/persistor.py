from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import object
import os
import shutil
import tarfile
import io
import json
from enum import Enum

import boto3
import botocore
from typing import Text
from rasa_nlu.config import RasaNLUConfig


def get_persistor(config):
    # type: (RasaNLUConfig) -> Persistor
    """Returns an instance of the requested persistor. Currently, `aws` and `gcs` are supported"""
    p = None
    if 'storage' not in config:
        raise KeyError(
            "No persistent storage specified. Supported values are {}".format(", ".join(['aws', 'gcs', 'mongodb'])))

    if config['storage'] == 'aws':
        p = AWSPersistor(config['path'], config['aws_region'], config['bucket_name'])
    elif config['storage'] == 'gcs':
        p = GCSPersistor(config['path'], config['bucket_name'])
    elif config['storage'] == 'mongodb':
        p = MongoDBPersistor(config['mongodb_uri'], config['collection_name'])
    return p


class PersistorType(Enum):
    FILESYSTEM = 1
    DATABASE = 2


class Persistor(object):
    """Store models in cloud and fetch them when needed"""

    def save_tar(self, target_dir):
        # type: (Text) -> None
        """Uploads a model persisted in the `target_dir` to cloud storage."""
        raise NotImplementedError("")

    def fetch_and_extract(self, filename):
        # type: (Text) -> None
        """Downloads a model that has previously been persisted to cloud storage."""
        raise NotImplementedError("")


class AWSPersistor(Persistor):
    """Store models on S3 and fetch them when needed instead of storing them on the local disk."""

    type = PersistorType.FILESYSTEM

    def __init__(self, data_dir, aws_region, bucket_name):
        # type: (Text, Text, Text) -> None
        Persistor.__init__(self)
        self.data_dir = data_dir
        self.s3 = boto3.resource('s3', region_name=aws_region)
        self.bucket_name = bucket_name
        try:
            self.s3.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={'LocationConstraint': aws_region})
        except botocore.exceptions.ClientError as e:
            pass  # bucket already exists
        self.bucket = self.s3.Bucket(bucket_name)

    def save_tar(self, target_dir):
        # type: (Text) -> None
        """Uploads a model persisted in the `target_dir` to s3."""

        if not os.path.isdir(target_dir):
            raise ValueError("Target directory '{}' not found.".format(target_dir))

        base_name = os.path.basename(target_dir)
        base_dir = os.path.dirname(target_dir)
        tarname = shutil.make_archive(base_name, 'gztar', root_dir=base_dir, base_dir=base_name)
        filekey = os.path.basename(tarname)
        self.s3.Object(self.bucket_name, filekey).put(Body=open(tarname, 'rb'))

    def fetch_and_extract(self, filename):
        # type: (Text) -> None
        """Downloads a model that has previously been persisted to s3."""

        with io.open(filename, 'wb') as f:
            self.bucket.download_fileobj(filename, f)
        with tarfile.open(filename, "r:gz") as tar:
            tar.extractall(self.data_dir)


class GCSPersistor(Persistor):
    """Store models on Google Cloud Storage and fetch them when needed instead of storing them on the local disk."""

    type = PersistorType.FILESYSTEM

    def __init__(self, data_dir, bucket_name):
        Persistor.__init__(self)
        from google.cloud import storage
        from google.cloud import exceptions
        self.data_dir = data_dir
        self.bucket_name = bucket_name
        self.storage_client = storage.Client()

        try:
            self.storage_client.create_bucket(bucket_name)
        except exceptions.Conflict as e:
            # bucket exists
            pass
        self.bucket = self.storage_client.bucket(bucket_name)

    def save_tar(self, target_dir):
        # type: (Text) -> None
        """Uploads a model persisted in the `target_dir` to GCS."""
        if not os.path.isdir(target_dir):
            raise ValueError('target_dir %r not found.' % target_dir)

        base_name = os.path.basename(target_dir)
        base_dir = os.path.dirname(target_dir)
        tarname = shutil.make_archive(base_name, 'gztar', root_dir=base_dir, base_dir=base_name)
        filekey = os.path.basename(tarname)
        blob = self.bucket.blob(filekey)
        blob.upload_from_filename(tarname)

    def fetch_and_extract(self, filename):
        # type: (Text) -> None
        """Downloads a model that has previously been persisted to GCS."""

        blob = self.bucket.blob(filename)
        blob.download_to_filename(filename)

        with tarfile.open(filename, "r:gz") as tar:
            tar.extractall(self.data_dir)


class MongoDBPersistor(Persistor):
    """Store models on MongoDB and fetch them when needed instead of storing them on the file system."""

    data_file_names = ['intent_classifier.pkl', 'metadata.json', 'entity_synonyms.json', 'training_data.json',
                       'ner/config.json', 'ner/model']
    type = PersistorType.DATABASE

    def __init__(self, mongo_uri, collection_name):
        Persistor.__init__(self)
        from pymongo import MongoClient
        client = MongoClient(mongo_uri)
        self.db = client.get_default_database()
        self.collection = self.db[collection_name]

    def save_tar(self, target_dir):
        # type: (Text) -> None
        """Uploads a model persisted in the `target_dir` to mongodb."""

        from bson.binary import Binary
        if not os.path.isdir(target_dir):
            raise ValueError("Target directory '{}' not found.".format(target_dir))
        base_name = os.path.basename(target_dir)
        data_dict = {'model_name': base_name}
        for file_name in MongoDBPersistor.data_file_names:
            file_loc = "{0}/{1}".format(target_dir, file_name)
            _, file_extension = os.path.splitext(file_name)
            if file_extension == '.json':
                with open(file_loc) as json_file:
                    json_data = json.load(json_file)
                    data_dict[file_name] = json_data
            else:
                with open(file_loc, 'rb') as pickle_file:
                    data_dict[file_name] = Binary(pickle_file.read(), 0)
        self.collection.insert(data_dict, check_keys=False)

    def fetch_and_extract(self, model_dir):
        # type: (Text) -> None
        """Downloads a model that has previously been persisted to mongodb."""

        model_name = os.path.basename(model_dir)
        base_dir = os.path.dirname(model_dir)
        data_dict = self.collection.find_one({'model_name': model_name})
        if not data_dict:
            raise ValueError("Collection does not contain a model for given name '{}'".format(model_name))
        data_dict.pop('_id')
        data_dict.pop('model_name')
        for (file_name, data) in data_dict.items():
            _, file_extension = os.path.splitext(file_name)
            file_loc = "{0}/{1}/{2}".format(base_dir, model_name, file_name)
            model_base_dir = os.path.dirname(file_loc)
            if not os.path.exists(model_base_dir):
                os.makedirs(model_base_dir)
            if file_extension == '.json':
                with open(file_loc, 'w') as json_file:
                    json.dump(data, json_file)
            else:
                with open(file_loc, 'wb') as pickle_file:
                    pickle_file.write(data)
