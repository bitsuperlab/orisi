from basehandler import BaseHandler
from password_db import (
    LockedPasswordTransaction,
    RSAKeyPairs,
    RightGuess,
    SentPasswordTransaction)
from util import Util

import base64
import hashlib
import json
import logging
import time

from decimal import Decimal

HEURISTIC_WAIT_TIME = 60*60

class GuessPasswordHandler(BaseHandler):
  def unknown_tx(self, pwtxid):
    if LockedPasswordTransaction(self.oracle.db).get_by_pwtxid(pwtxid):
      return False
    return True

  def transaction_done(self, pwtxid):
    transaction = LockedPasswordTransaction(self.oracle.db).get_by_pwtxid(pwtxid)
    return transaction['done'] == 1

  def decrypt_message(self, pwtxid, base64_msg):
    msg = base64.decodestring(base64_msg)
    rsa_key = Util.construct_key_from_data(
        RSAKeyPairs(self.oracle.db).get_by_pwtxid(pwtxid))
    message = rsa_key.decrypt(msg)
    return message

  def guess_is_right(self, pwtxid, guess):
    message = self.decrypt_message(pwtxid, guess)
    try:
      message = json.loads(message)
    except ValueError:
      return False

    if not 'pass_hash' in message or not 'address' in message:
      return False

    pass_hash = message['pass_hash']

    transaction = LockedPasswordTransaction(self.oracle.db).get_by_pwtxid(pwtxid)
    details = json.loads(transaction['json_data'])

    original_hash = details['password_hash']

    if pass_hash == original_hash:
      return True
    return False

  def get_address(self, pwtxid, guess):
    # Assumes guess_is_right was already called and all the data is correct
    message = self.decrypt_message(pwtxid, guess)
    message = json.loads(message)
    return message['address']

  def handle_request(self, request):
    message = request.message
    message = json.loads(message)

    pwtxid = message['pwtxid']
    rsa_key = RSAKeyPairs(self.oracle.db).get_by_pwtxid(pwtxid)
    rsa_hash = hashlib.sha256(rsa_key['public']).hexdigest()

    if not rsa_hash in message['passwords']:
      logging.info('guess doesn\'t apply to me')
      return

    if self.unknown_tx(pwtxid):
      logging.info('unknown transaction')
      return

    if self.transaction_done(pwtxid):
      logging.info('transaction_locked')
      return

    guess = message['passwords'][rsa_hash]

    if self.guess_is_right(pwtxid, guess):
      # Create RightGuess, create task
      guess_time = request.received_time_epoch
      guess_dict = {
          'pwtxid': pwtxid,
          'guess': guess,
          'received_time': guess_time
      }
      RightGuess(self.oracle.db).save(guess_dict)
      self.oracle.task_queue.save({
          'operation': 'guess_password',
          'filter_field': 'guess:{}'.format(pwtxid),
          'done':0,
          'next_check': int(time.time()) + HEURISTIC_WAIT_TIME,
          'json_data': json.dumps(guess_dict)})

  def get_rqhs_of_future_transaction(self, transaction, locktime):
    inputs, outputs = self.oracle.get_inputs_outputs([transaction])
    future_hash = {
        'inputs': inputs,
        'outputs': outputs,
        'locktime': locktime,
        'condition': 'True'
    }
    future_hash = hashlib.sha256(json.dumps(future_hash)).hexdigest()
    return future_hash

  def handle_task(self, task):
    data = json.loads(task['json_data'])
    pwtxid = data['pwtxid']
    address = self.get_address(pwtxid, data['guess'])
    transaction = LockedPasswordTransaction(self.oracle.db).get_by_pwtxid(pwtxid)
    if transaction['done'] == 1:
      logging.info('someone was faster')
      return
    LockedPasswordTransaction(self.oracle.db).mark_as_done(pwtxid)

    message = json.loads(transaction['json_data'])
    prevtx = message['prevtx']
    locktime = message['locktime']
    outputs = message['oracle_fees']
    sum_amount = Decimal(message['sum_amount'])
    miners_fee = Decimal(message['miners_fee'])
    available_amount = sum_amount - miners_fee
    future_transaction = Util.create_future_transaction(
        self.oracle.btc,
        prevtx,
        outputs,
        available_amount,
        address,
        locktime)

    # Code repetition, should be removed!
    future_hash = self.get_rqhs_of_future_transaction(future_transaction, locktime)

    if len(self.oracle.task_queue.get_by_filter('rqhs:{}'.format(future_hash))) > 0:
      logging.info("transaction already pushed")
      return

    self.oracle.btc.add_multisig_address(message['req_sigs'], message['pubkey_json'])

    signed_transaction = self.oracle.btc.sign_transaction(future_transaction, prevtx)

    # Prepare request corresponding with protocol
    request = {
        "transactions": [
            {"raw_transaction":signed_transaction, "prevtx": prevtx},],
        "locktime": 0,
        "condition": "True",
        "pubkey_json": message['pubkey_json'],
        "req_sigs": message['req_sigs'],
        "operation": 'conditioned_transaction'
    }
    request = json.dumps(request)
    self.oracle.communication.broadcast('conditioned_transaction', request)
    LockedPasswordTransaction(self.oracle.db).mark_as_done(pwtxid)
    self.oracle.task_queue.done(task)
    SentPasswordTransaction(self.oracle.db).save({
        "pwtxid": pwtxid,
        "rqhs": future_hash,
        "tx": signed_transaction
    })

  def filter_tasks(self, task):
    def faster_task(t1, t2):
      d1 = json.loads(t1['json_data'])
      d2 = json.loads(t2['json_data'])
      if d1['received_time'] < d2['received_time']:
        return t1
      return t2
    tasks = self.oracle.task_queue.get_similar_ignore_check(task)
    final_task = reduce(faster_task, tasks)
    for t in tasks:
      if t != final_task:
        self.oracle.task_queue.done(t)
    return [final_task]