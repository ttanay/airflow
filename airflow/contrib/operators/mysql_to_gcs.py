# -*- coding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import json
import time

from airflow.contrib.hooks.gcs_hook import GoogleCloudStorageHook
from airflow.hooks.mysql_hook import MySqlHook
from airflow.models import BaseOperator
from airflow.utils.decorators import apply_defaults
from datetime import date, datetime
from decimal import Decimal
from MySQLdb.constants import FIELD_TYPE
from tempfile import NamedTemporaryFile
from six import string_types
import unicodecsv as csv

PY3 = sys.version_info[0] == 3


class MySqlToGoogleCloudStorageOperator(BaseOperator):
    """
    Copy data from MySQL to Google cloud storage in JSON or CSV format.
    """
    template_fields = ('sql', 'bucket', 'filename', 'schema_filename', 'schema')
    template_ext = ('.sql',)
    ui_color = '#a0e08c'

    @apply_defaults
    def __init__(self,
                 sql,
                 bucket,
                 filename,
                 schema_filename=None,
                 approx_max_file_size_bytes=1900000000,
                 mysql_conn_id='mysql_default',
                 google_cloud_storage_conn_id='google_cloud_storage_default',
                 schema=None,
                 delegate_to=None,
                 export_format={'file_format': 'json'},
                 *args,
                 **kwargs):
        """
        :param sql: The SQL to execute on the MySQL table.
        :type sql: string
        :param bucket: The bucket to upload to.
        :type bucket: string
        :param filename: The filename to use as the object name when uploading
            to Google cloud storage. A {} should be specified in the filename
            to allow the operator to inject file numbers in cases where the
            file is split due to size.
        :type filename: string
        :param schema_filename: If set, the filename to use as the object name
            when uploading a .json file containing the BigQuery schema fields
            for the table that was dumped from MySQL.
        :type schema_filename: string
        :param approx_max_file_size_bytes: This operator supports the ability
            to split large table dumps into multiple files (see notes in the
            filenamed param docs above). Google cloud storage allows for files
            to be a maximum of 4GB. This param allows developers to specify the
            file size of the splits.
        :type approx_max_file_size_bytes: long
        :param mysql_conn_id: Reference to a specific MySQL hook.
        :type mysql_conn_id: string
        :param google_cloud_storage_conn_id: Reference to a specific Google
            cloud storage hook.
        :type google_cloud_storage_conn_id: string
        :param schema: The schema to use, if any. Should be a list of dict or
            a str. Examples could be see: https://cloud.google.com/bigquery
            /docs/schemas#specifying_a_json_schema_file
        :type schema: str or list
        :param delegate_to: The account to impersonate, if any. For this to
            work, the service account making the request must have domain-wide
            delegation enabled.
        :param export_format: Details for files to be exported into GCS.
            Allows to specify 'json' or 'csv', and also addiitional details for
            CSV file exports (quotes, separators, etc.)
            This is a dict with the following key-value pairs:
              * file_format: 'json' or 'csv'. If using CSV, more details can
                              be added
              * csv_dialect: preconfigured set of CSV export parameters
                             (i.e.: 'excel', 'excel-tab', 'unix_dialect').
                             If present, will ignore all other 'csv_' options.
                             See https://docs.python.org/3/library/csv.html
              * csv_delimiter: A one-character string used to separate fields.
                               It defaults to ','.
              * csv_doublequote: If doublequote is False and no escapechar is set,
                                 Error is raised if a quotechar is found in a field.
                                 It defaults to True.
              * csv_escapechar: A one-character string used to escape the delimiter
                                if quoting is set to QUOTE_NONE and the quotechar
                                if doublequote is False.
                                It defaults to None, which disables escaping.
              * csv_lineterminator: The string used to terminate lines.
                                    It defaults to '\r\n'.
              * csv_quotechar: A one-character string used to quote fields
                                containing special characters, such as the delimiter
                                or quotechar, or which contain new-line characters.
                                It defaults to '"'.
              * csv_quoting: Controls when quotes should be generated.
                             It can take on any of the QUOTE_* constants
                             Defaults to csv.QUOTE_MINIMAL.
                             Valid values are:
                             'csv.QUOTE_ALL': Quote all fields
                             'csv.QUOTE_MINIMAL': only quote those fields which contain
                                                    special characters such as delimiter,
                                                    quotechar or any of the characters
                                                    in lineterminator.
                             'csv.QUOTE_NONNUMERIC': Quote all non-numeric fields.
                             'csv.QUOTE_NONE': never quote fields. When the current
                                                delimiter occurs in output data it is
                                                preceded by the current escapechar
                                                character. If escapechar is not set,
                                                the writer will raise Error if any
                                                characters that require escaping are
                                                encountered.
              * csv_columnheader: If True, first row in the file will include column
                                  names. Defaults to False.
        """
        super(MySqlToGoogleCloudStorageOperator, self).__init__(*args, **kwargs)
        self.sql = sql
        self.bucket = bucket
        self.filename = filename
        self.schema_filename = schema_filename
        self.approx_max_file_size_bytes = approx_max_file_size_bytes
        self.mysql_conn_id = mysql_conn_id
        self.google_cloud_storage_conn_id = google_cloud_storage_conn_id
        self.schema = schema
        self.delegate_to = delegate_to
        self.export_format = export_format

    def execute(self, context):
        cursor = self._query_mysql()
        files_to_upload = self._write_local_data_files(cursor)

        # If a schema is set, create a BQ schema JSON file.
        if self.schema_filename:
            files_to_upload.update(self._write_local_schema_file(cursor))

        # Flush all files before uploading
        for file_handle in files_to_upload.values():
            file_handle.flush()

        self._upload_to_gcs(files_to_upload)

        # Close all temp file handles.
        for file_handle in files_to_upload.values():
            file_handle.close()

    def _query_mysql(self):
        """
        Queries mysql and returns a cursor to the results.
        """
        mysql = MySqlHook(mysql_conn_id=self.mysql_conn_id)
        conn = mysql.get_conn()
        cursor = conn.cursor()
        cursor.execute(self.sql)
        return cursor

    def _write_local_data_files(self, cursor):
        """
        Takes a cursor, and writes results to a local file.

        :return: A dictionary where keys are filenames to be used as object
            names in GCS, and values are file handles to local files that
            contain the data for the GCS objects.
        """
        schema = list(map(lambda schema_tuple: schema_tuple[0], cursor.description))
        file_no = 0
        tmp_file_handle = NamedTemporaryFile(delete=True)
        tmp_file_handles = {self.filename.format(file_no): tmp_file_handle}

        # Save file header for csv if required
        if(self.export_format['file_format'] == 'csv'):

            # nit(ttanay): Can be made into a configure_csv function?
            #   PS: only for the dialect part. (Maybe a classmethod?)
            # Deal with CSV formatting. Try to use dialect if passed
            if('csv_dialect' in self.export_format):
                # Use dialect name from params
                dialect_name = self.export_format['csv_dialect']
            else:
                # Create internal dialect based on parameters passed
                dialect_name = 'mysql_to_gcs'
                # TODO(ttanay): check if there's a better way to do multi-line fns
                # TODO(ttanay): Find a way to update the kwargs based on export
                # format and pass that dict as kwargs to the register_dialect fn
                csv.register_dialect(dialect_name,
                                     delimiter=self.export_format.get(
                                         'csv_delimiter', ','),
                                     doublequote=self.export_format.get(
                                         'csv_doublequote', True),
                                     escapechar=self.export_format.get(
                                         'csv_escapechar', None),
                                     lineterminator=self.export_format.get(
                                         'csv_lineterminator', '\r\n'),
                                     quotechar=self.export_format.get(
                                         'csv_quotechar', '"'),
                                     quoting=self.export_format.get(
                                         'csv_quoting', csv.QUOTE_MINIMAL))
            # Create CSV writer using either provided or generated dialect
            csv_writer = csv.writer(tmp_file_handle,
                                    encoding='utf-8',
                                    dialect=dialect_name)

            # nit(ttanay): The user will need to specify this config each time.
            #   Otherwise, it will be missed out. Should the headers be specified?
            #   Check with BigQuery file loads as well.
            # Include column header in first row
            if('csv_columnheader' in self.export_format and
                    eval(self.export_format['csv_columnheader'])):
                csv_writer.writerow(schema)

        for row in cursor:
            # Convert datetimes and longs to BigQuery safe types
            row = map(self.convert_types, row)

            # Save rows as CSV
            if(self.export_format['file_format'] == 'csv'):
                csv_writer.writerow(row)
            # Save rows as JSON
            else:
                # Convert datetime objects to utc seconds, and decimals to floats
                row_dict = dict(zip(schema, row))

                # TODO validate that row isn't > 2MB. BQ enforces a hard row size of 2MB.
                s = json.dumps(row_dict, sort_keys=True)
                if PY3:
                    s = s.encode('utf-8')
                tmp_file_handle.write(s)

                # Append newline to make dumps BigQuery compatible.
                tmp_file_handle.write(b'\n')

            # Stop if the file exceeds the file size limit.
            if tmp_file_handle.tell() >= self.approx_max_file_size_bytes:
                file_no += 1
                tmp_file_handle = NamedTemporaryFile(delete=True)
                tmp_file_handles[self.filename.format(file_no)] = tmp_file_handle

                # For CSV files, weed to create a new writer with the new handle
                # and write header in first row
                if(self.export_format['file_format'] == 'csv'):
                    csv_writer = csv.writer(tmp_file_handle,
                                            encoding='utf-8',
                                            dialect=dialect_name)
                    if('csv_columnheader' in self.export_format and
                            eval(self.export_format['csv_columnheader'])):
                        csv_writer.writerow(schema)

        return tmp_file_handles

    def _write_local_schema_file(self, cursor):
        """
        Takes a cursor, and writes the BigQuery schema for the results to a
        local file system.

        :return: A dictionary where key is a filename to be used as an object
            name in GCS, and values are file handles to local files that
            contains the BigQuery schema fields in .json format.
        """
        schema = []
        tmp_schema_file_handle = NamedTemporaryFile(delete=True)
        if self.schema is not None and isinstance(self.schema, string_types):
            schema = self.schema
            tmp_schema_file_handle.write(schema)
        else:
            if self.schema is not None and isinstance(self.schema, list):
                schema = self.schema
            else:
                for field in cursor.description:
                    # See PEP 249 for details about the description tuple.
                    field_name = field[0]
                    field_type = self.type_map(field[1])
                    # Always allow TIMESTAMP to be nullable. MySQLdb returns None types
                    # for required fields because some MySQL timestamps can't be
                    # represented by Python's datetime (e.g. 0000-00-00 00:00:00).
                    if field[6] or field_type == 'TIMESTAMP':
                        field_mode = 'NULLABLE'
                    else:
                        field_mode = 'REQUIRED'
                    schema.append({
                        'name': field_name,
                        'type': field_type,
                        'mode': field_mode,
                    })
            # WON'T WORK. dumps doesn't take a file pointer.
            s = json.dumps(schema, tmp_schema_file_handle, sort_keys=True)
            if PY3:
                s = s.encode('utf-8')
            tmp_schema_file_handle.write(s)

        self.log.info('Using schema for %s: %s', self.schema_filename, schema)
        return {self.schema_filename: tmp_schema_file_handle}

    def _upload_to_gcs(self, files_to_upload):
        """
        Upload all of the file splits (and optionally the schema .json file) to
        Google cloud storage.
        """
        # Compose mime_type using file format passed as param
        # TODO(ttanay): Find correct MIME type for CSV files.
        mime_type = 'application/' + self.export_format['file_format']
        hook = GoogleCloudStorageHook(
            google_cloud_storage_conn_id=self.google_cloud_storage_conn_id,
            delegate_to=self.delegate_to)
        for object, tmp_file_handle in files_to_upload.items():
            hook.upload(self.bucket, object, tmp_file_handle.name, mime_type)

    @classmethod
    def convert_types(cls, value):
        """
        Takes a value from MySQLdb, and converts it to a value that's safe for
        JSON/Google cloud storage/BigQuery. Dates are converted to UTC seconds.
        Decimals are converted to floats.
        """
        if type(value) in (datetime, date):
            return time.mktime(value.timetuple())
        elif isinstance(value, Decimal):
            return float(value)
        else:
            return value

    @classmethod
    def type_map(cls, mysql_type):
        """
        Helper function that maps from MySQL fields to BigQuery fields. Used
        when a schema_filename is set.
        """
        d = {
            FIELD_TYPE.INT24: 'INTEGER',
            FIELD_TYPE.TINY: 'INTEGER',
            FIELD_TYPE.BIT: 'INTEGER',
            FIELD_TYPE.DATETIME: 'TIMESTAMP',
            FIELD_TYPE.DATE: 'TIMESTAMP',
            FIELD_TYPE.DECIMAL: 'FLOAT',
            FIELD_TYPE.NEWDECIMAL: 'FLOAT',
            FIELD_TYPE.DOUBLE: 'FLOAT',
            FIELD_TYPE.FLOAT: 'FLOAT',
            FIELD_TYPE.INT24: 'INTEGER',
            FIELD_TYPE.LONG: 'INTEGER',
            FIELD_TYPE.LONGLONG: 'INTEGER',
            FIELD_TYPE.SHORT: 'INTEGER',
            FIELD_TYPE.TIMESTAMP: 'TIMESTAMP',
            FIELD_TYPE.YEAR: 'INTEGER',
        }
        return d[mysql_type] if mysql_type in d else 'STRING'
