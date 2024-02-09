# celery_task.py
from celery import Celery
import sqlite3
from selenium_tasks import SeleniumProcesses
from datetime import datetime
import odoorpc
import xmlrpc.client as xc
import os
import base64
import shutil
import requests
import json

celery = Celery(
    'celery_tasks',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/0',
)

from config import DATABASE, selenium_config


def connect_odoo_rpc():
    # ODOORPC CREDENTIALS
    odoo_connection_config = selenium_config.get('staging_config')
    if odoo_connection_config:
        odoo_username = odoo_connection_config.get('ODOO_USERNAME')
        odoo_password = odoo_connection_config.get('ODOO_PASSWORD')
        odoo_url = odoo_connection_config.get('ODOO_URL')
        odoo_port = odoo_connection_config.get('ODOO_PORT')
        odoo_db = odoo_connection_config.get('ODOO_DATABASE')
        use_odoo_rpc = odoo_connection_config.get('use_odoo_rpc')

        try:
            if use_odoo_rpc:
                odoo = odoorpc.ODOO(odoo_url, port=odoo_port)
                odoo.login(odoo_db, odoo_username, odoo_password)
                return True, odoo
            else:
                sock_common = xc.ServerProxy(odoo_url + '/xmlrpc/common', allow_none=True)
                uid = sock_common.login(odoo_db, odoo_username, odoo_password)
                sock = xc.ServerProxy(odoo_url + '/xmlrpc/object', allow_none=True)
                return True, [uid, sock]
        except Exception as e:
            return False, e
    return False, "Odoo Connection Credentials not found."


def upload_document(vals):
    download_directory = selenium_config.get('DOWNLOAD_DIRECTORY')
    move_path = selenium_config.get('MOVE_PATH')

    picking_id = vals.get('picking')
    file_name = f"Documents for Order {vals.get('order_name')}"

    files_in_directory = os.listdir(download_directory)
    found_file_path = None
    found_file = None
    for file in files_in_directory:
        if file_name in file:
            found_file_path = os.path.join(download_directory, file)
            found_file = file
            break

    if found_file_path:
        success, odoo_obj = connect_odoo_rpc()
        if success:
            odoo_connection_config = selenium_config.get('local_config')
            use_odoo_rpc = odoo_connection_config.get('use_odoo_rpc')
            odoo_password = odoo_connection_config.get('ODOO_PASSWORD')
            odoo_db = odoo_connection_config.get('ODOO_DATABASE')
            try:
                if use_odoo_rpc:
                    picking_obj = odoo_obj.env['stock.picking']
                    with open(found_file_path, "rb") as zip_file:
                        data = zip_file.read()
                        picking_obj.write([int(picking_id)],
                                          {'goflow_document': base64.b64encode(data or b'').decode("ascii")})
                else:
                    uid = odoo_obj[0]
                    sock = odoo_obj[1]
                    with open(found_file_path, "rb") as zip_file:
                        data = zip_file.read()
                        sock.execute(odoo_db, uid, odoo_password, 'stock.picking', 'write', int(picking_id),
                                     {'goflow_document': base64.b64encode(data or b'').decode("ascii")})
            except Exception as e:
                raise Exception(f"Error in uploading document: {e}")
        else:
            raise Exception(f"Error while Odoo rpc connection: {odoo_obj}")
        try:
            if not os.path.exists(move_path):
                os.mkdir(move_path)
            shutil.move(found_file_path, move_path + '/' + found_file)
        except Exception as e:
            raise Exception(f"Error in moving file {e}")
        return "File Uploaded Successfully"
    else:
        raise Exception("File Not Found")


def update_status_to_odoo(vals):
    try:
        odoo_connection_config = selenium_config.get('staging_config')
        url = odoo_connection_config.get('ODOO_WEBHOOK_URL')
        payload = json.dumps(vals)
        headers = {
            'API-KEY': odoo_connection_config.get('AUTH_KEY'),
            'Content-Type': 'application/json'
        }
        response = requests.request("POST", url, headers=headers, data=payload)
        return True, str(response.text)
    except Exception as e:
        return False, f"Status update failed to ODOO. ERROR: {str(e)}"


def main_process(task_id, db, cron_db_id):
    log = []
    start_time = datetime.now()
    cursor = db.execute('UPDATE packaging_order SET status = ? WHERE ID = ?', ('processing', task_id))
    db.commit()
    log.append(f'<p>Process started at {str(start_time)}.<p>')
    try:
        cursor = db.execute('SELECT * FROM packaging_order WHERE ID = ?', (task_id,))
        user_row = cursor.fetchone()
        columns = [col[0] for col in cursor.description]
        user_dict = dict(zip(columns, user_row))

        odoo_vals = {'order_ref': user_dict.get('ID'), 'rpa_status': False}

        selenium = SeleniumProcesses()
        selenium.log = list(log)
        success, selenium_exception, msg = selenium.process_order(user_dict, cron_db_id)

        if success:

            selenium.log.append(f"<p>Selenium Process completed at {str(datetime.now())}</p>")

            try:
                res = upload_document(user_dict)
                selenium.log.append(f"<p>{res}</p>")
                odoo_vals.update({'status': 'doc_generated'})
                cursor.execute('UPDATE packaging_order SET status = ?, log = ? WHERE ID = ?',
                               ('completed', " ".join(selenium.log), task_id))
                db.commit()
            except Exception as e:
                selenium.log.append(f"<p>Error in uploading document {e}</p>")
                odoo_vals.update({'status': 'doc_generated_not_uploaded'})
                cursor.execute('UPDATE packaging_order SET status = ?, log = ? WHERE ID = ?',
                               ('completed but document not uploaded', " ".join(selenium.log), task_id))
                db.commit()

            odoo_vals.update({'log': " ".join(selenium.log)})

            call_successful, message = update_status_to_odoo(odoo_vals)

            cursor.execute(
                'UPDATE packaging_order SET status_updated_to_odoo = ?, odoo_response_message = ? WHERE ID = ?',
                (call_successful, message, task_id))
            db.commit()
        else:

            selenium.log.append(
                f"<p>Selenium Process Error: {selenium_exception} {msg}. Completed at {str(datetime.now())}</p>")
            odoo_vals.update({'status': 'require_manual_shipment'})
            cursor.execute('UPDATE packaging_order SET error = ?, msg = ?, log = ? WHERE ID = ?',
                           (str(selenium_exception), str(msg), " ".join(selenium.log), task_id))
            db.commit()

            odoo_vals.update({'log': " ".join(selenium.log)})

            call_successful, message = update_status_to_odoo(odoo_vals)

            cursor.execute(
                'UPDATE packaging_order SET status_updated_to_odoo = ?, odoo_response_message = ? WHERE ID = ?',
                (call_successful, message, task_id))
            db.commit()

            raise selenium_exception
    except Exception as e:
        cursor.execute('UPDATE packaging_order SET status = ?, celery_error = ? WHERE ID = ?',
                       ('failed', str(e), task_id))
        db.commit()

    db.commit()


@celery.task
def process_cron(cron):
    cron_db_id = int(cron.split('_')[1])
    db = sqlite3.connect(DATABASE)
    cursor = db.execute('SELECT * FROM packaging_order WHERE status = ? AND cron = ? ORDER BY create_date ASC LIMIT 1',
                        ('pending', cron))
    pending_task = cursor.fetchone()

    if pending_task:
        task_id = pending_task[0]
        main_process(task_id, db, cron_db_id)

        cursor = db.execute(
            'SELECT * FROM packaging_order WHERE status = ? AND cron = ? ORDER BY create_date ASC LIMIT 1',
            ('pending', cron))
        pending_task = cursor.fetchone()

        if pending_task:
            process_cron.delay(cron)
        else:
            cursor.execute('UPDATE status_boolean_table SET status = ? WHERE ID = ?', (False, cron_db_id))
            db.commit()
    else:
        cursor.execute('UPDATE status_boolean_table SET status = ? WHERE ID = ?', (False, cron_db_id))
        db.commit()


if __name__ == '__main__':
    celery.start()
