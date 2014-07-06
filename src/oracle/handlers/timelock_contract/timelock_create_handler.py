from basehandler import BaseHandler
from oracle.oracle_db import SignedTransaction, UsedInput

import json
import logging

from xmlrpclib import ProtocolError

HEURISTIC_ADD_TIME = 60 * 3

class TransactionVerificationError(Exception):
  pass

class ConditionedTransactionHandler(BaseHandler):
  def __init__(self, oracle):
    self.oracle = oracle

  def handle_task(self, task):
    body = json.loads(task['json_data'])
    tx = body['transaction']

    signed_transaction = self.oracle.btc.sign_transaction(tx['raw_transaction'], tx['prevtx'])
    body['transaction']['raw_transaction'] = signed_transaction

    SignedTransaction(self.oracle.db).save({
        "hex_transaction": signed_transaction,
        "prevtx":json.dumps(tx['prevtx'])})

    self.oracle.communication.broadcast_signed_transaction(json.dumps(body))
    self.oracle.task_queue.done(task)

  def handle_request(self, request):
    body = json.loads(request.message)

    pubkey_list = body['pubkey_list']
    try:
      req_sigs = int(body['req_sigs'])
    except ValueError:
      logging.debug("req_sigs must be a number")
      return

    try:
      locktime = int(body['locktime'])
    except ValueError:
      logging.debug("locktime must be a number")
      return

    try:
      self.oracle.btc.add_multisig_address(req_sigs, pubkey_list)
    except ProtocolError:
      logging.debug("cant add multisig address")
      return

    tx = body['transaction']
    if not self.is_proper_transaction(tx):
      return

    raw_transaction = tx['raw_transaction']
    all_inputs, all_outputs = self.oracle.get_inputs_outputs([raw_transaction])

    rq_hash = self.get_request_hash(body)

    used_input_db = UsedInput(self.oracle.db)
    for i in all_inputs:
      used_input = used_input_db.get_input(i)
      if used_input and used_input["json_out"] != rq_hash:
          self.oracle.communication.broadcast(
              'AddressDuplicate',
              'this multisig address was already used')
          return
    for i in all_inputs:
      used_input_db.save({
          'input_hash': i,
          'json_out': rq_hash
      })

    prevtx = tx['prevtx']
    turns = [self.get_my_turn(ptx['redeemScript']) for ptx in prevtx if 'redeemScript' in tx]
    
    my_turn = max(turns)
    add_time = my_turn * HEURISTIC_ADD_TIME

    self.oracle.task_queue.save({
        "operation": 'conditioned_transaction',
        "json_data": request.message,
        "filter_field": 'rqhs:{}'.format(rq_hash),
        "done": 0,
        "next_check": locktime + add_time
    })