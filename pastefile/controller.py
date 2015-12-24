#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
import magic
import datetime
import logging
from pastefile import utils
from jsondb import JsonDB
from flask import send_from_directory, abort
from werkzeug import secure_filename

LOG = logging.getLogger(__name__)


def get_infos_file_from_md5(md5, dbfile):
    with JsonDB(dbfile=dbfile) as db:
        if db.lock_error:
            return False
        return db.read(md5)


def clean_files(dbfile, expire=86400):
    with JsonDB(dbfile=dbfile) as db:
        if db.lock_error:
            LOG.warning('Cant clean files')
            return False
        for k, v in list(db.db.iteritems()):
            if int(db.db[k]['timestamp']) < int(time.time() -
               int(expire)):
                try:
                    os.remove(db.db[k]['storage_full_filename'])
                except OSError:
                    LOG.critical('Error while trying to remove %s'
                                 % db.db[k]['storage_full_filename'])
                if not os.path.isfile(db.db[k]['storage_full_filename']):
                    db.delete(k)


def get_file_info(id_file, config, env=None):
    infos = get_infos_file_from_md5(md5=id_file, dbfile=config['FILE_LIST'])
    if not infos:
        return False
    try:
        size = utils.human_readable(os.stat(infos['storage_full_filename']).st_size)
        expire = datetime.datetime.fromtimestamp(
                      int(infos['timestamp']) +
                      int(config['EXPIRE'])).strftime('%d-%m-%Y %H:%M:%S'),
        file_infos = {
            'name': infos['real_name'],
            'md5': id_file,
            'burn_after_read': infos['burn_after_read'],
            'timestamp': infos['timestamp'],
            'expire': expire,
            'type': magic.from_file(infos['storage_full_filename']),
            'size': size,
            'url': "%s/%s" % (utils.build_base_url(env=env), id_file)
        }
        return file_infos
    except:
        LOG.error('Unable to gather infos for file %s' % id_file)
        return False


def add_new_file(filename, source, dest, db, md5, burn_after_read):

    # If no lock, return false
    if db.lock_error:
        return False

    # IMPROVE : possible "bug" If a file is already uploaded, the burn_after_read
    #           Will not bu updated
    # File already exist, return True
    if md5 in db.db:
        try:
            os.remove(source)
        except OSError as e:
            LOG.error("Can't remove tmp file: %s" % e)
        return True

    try:
        os.rename(source, dest)
    except OSError as e:
        LOG.error("Can't move processing file to storage directory: %s" % e)
        return False

    db.write(md5, {
        'real_name': filename,
        'storage_full_filename': dest,
        'timestamp': int(time.time()),
        'burn_after_read': str(burn_after_read),
    })
    return True


def upload_file(request, config):
    value_burn_after_read = request.form.getlist('burn')
    if value_burn_after_read:
        burn_after_read = True
    else:
        burn_after_read = False

    # Write tmp file on disk
    try:
        file_md5, tmp_full_filename = utils.write_tmpfile_to_disk(file=request.files['file'],
                                                                  dest_dir=config['TMP_FOLDER'])
    except IOError:
        return 'Server error, contact administrator\n'

    secure_name = secure_filename(request.files['file'].filename)

    with JsonDB(dbfile=config['FILE_LIST']) as db:

        # Just inform for debug purpose
        if db.lock_error:
            LOG.error("Unable to get lock during file upload %s" % file_md5)

        # Try to write file on disk and db. Return false if file is not writed
        storage_full_filename = os.path.join(config['UPLOAD_FOLDER'], file_md5)
        succed_add_file = add_new_file(filename=secure_name,
                                       source=tmp_full_filename,
                                       dest=storage_full_filename,
                                       db=db,
                                       md5=file_md5,
                                       burn_after_read=burn_after_read)

    if not succed_add_file:
        # In the case the file is not in db, we have 2 reason :
        #  * We was not able to have the lock and write the file in the db.
        #  * Or an error occure during the file processing
        # In any case just tell the user to try later
        try:
            os.remove(tmp_full_filename)
        except OSError as e:
            LOG.error("Can't remove tmp file: %s" % e)

        LOG.info('Unable lock the db and find the file %s in db during upload' % file_md5)
        return 'Unable to upload the file, try again later ...\n'

    LOG.info("[POST] Client %s has successfully uploaded: %s (%s)"
             % (request.remote_addr, storage_full_filename, file_md5))
    return "%s/%s\n" % (utils.build_base_url(env=request.environ),
                        file_md5)


def delete_file(request, id_file, dbfile):
    with JsonDB(dbfile=dbfile) as db:
        if db.lock_error:
            return "Lock timed out\n"
        if id_file not in db.db:
            return abort(404)
        try:
            storage_full_filename = db.db[id_file]['storage_full_filename']
            os.remove(storage_full_filename)
            LOG.info("[DELETE] Client %s has deleted: %s (%s)"
                     % (request.remote_addr, db.db[id_file]['real_name'], id_file))
            db.delete(id_file)
            return "File %s deleted\n" % id_file
        except IOError as e:
            LOG.critical("Can't remove file: %s" % e)
            return "Error: %s\n" % e


def get_file(request, id_file, config):
    with JsonDB(dbfile=config['FILE_LIST']) as db:
        if db.lock_error:
            return "Lock timed out\n"
        if id_file not in db.db:
            return abort(404)

        filename = os.path.basename(db.db[id_file]['storage_full_filename'])
        LOG.info("[GET] Client %s has requested: %s (%s)"
                 % (request.remote_addr, db.db[id_file]['real_name'], id_file))

        if not os.path.isabs(config['UPLOAD_FOLDER']):
            path = "%s/%s" % (os.path.dirname(config['instance_path']),
                              config['UPLOAD_FOLDER'])
        else:
            path = config['UPLOAD_FOLDER']

        return send_from_directory(path,
                                   filename,
                                   attachment_filename=db.db[id_file]['real_name'],
                                   as_attachment=True)


def get_all_files(request, config):
    files_list_infos = {}
    with JsonDB(dbfile=config['FILE_LIST'],
                logger=config['LOGGER_NAME']) as db:
        if db.lock_error:
            return "Lock timed out\n"
        instant_db = db.db
    for k, v in instant_db.iteritems():
        _infos = get_file_info(id_file=k,
                               config=config,
                               env=request.environ)
        if not _infos:
            continue
        files_list_infos[k] = _infos
    return files_list_infos
