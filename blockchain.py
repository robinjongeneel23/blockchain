from functools import reduce
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound
from time import time
import json
import requests

from utility.hash_util import hash_block
from utility.verification import Verification
from transaction import Transaction
from wallet import Wallet
from utility.database import Session
from block import Block
from peer_nodes import Node

# The reward we give to miners (for creating a new block)
MINING_REWARD = 10


class Blockchain:
    """The Blockchain class manages the chain of blocks as well as open
    transactions and the node on which it's running.

    Attributes:
        :chain: The list of blocks
        :open_transactions (private): The list of open transactions
        :hosting_node: The connected node (which runs the blockchain).
    """

    def __init__(self, public_key, node_id):
        """The constructor of the Blockchain class."""

        # Initializing our (empty) blockchain list
        self.__chain = []
        # Unhandled transactions
        self.__open_transactions = []
        # handled transactions
        self.__mined_transactions = []
        self.public_key = public_key
        self.__peer_nodes = []
        self.node_id = node_id
        self.resolve_conflicts = False
        self.load_data()

    # This turns the chain attribute into a property with a getter (the method
    # below) and a setter (@chain.setter)
    @property
    def chain(self):
        return self.__chain[:]

    # The setter for the chain property
    @chain.setter
    def chain(self, val):
        self.__chain = val

    @property
    def mined_transactions(self):
        return self.__mined_transactions[:]

    # The setter for the non-open transactions property
    @mined_transactions.setter
    def mined_transactions(self, val):
        self.__mined_transactions = val

    def get_open_transactions(self):
        """Returns a copy of the open transactions list."""
        return self.__open_transactions[:]

    def load_data(self):
        """Initialize blockchain + open transactions data from a file."""

        session = Session()
        blockchain_objects = session.query(Block).all()
        blockchain = self.make_sendable_list(blockchain_objects)

        all_mined_transactions_objects = session.query(Transaction)\
            .filter(Transaction.mined == 1).all()
        all_mined_transactions = self.make_sendable_list(all_mined_transactions_objects)

        session.close()
        self.chain = blockchain
        self.mined_transactions = all_mined_transactions

        if len(blockchain) == 0:
            # Our starting block for the blockchain
            session = Session()
            genesis_block = Block(0, 'GENESIS', 'GENESIS', 100, -1)
            session.add(genesis_block)
            session.commit()
            self.chain = session.query(Block).all()
            session.close()

        session = Session()
        open_transactions_objects = session.query(Transaction)\
            .filter(Transaction.mined == 0).all()
        open_transactions = self.make_sendable_list(open_transactions_objects)
        self.__open_transactions = open_transactions

        peer_nodes_objects = session.query(Node).all()
        peer_nodes = self.make_sendable_list(peer_nodes_objects)
        self.__peer_nodes = peer_nodes
        session.close()

    def proof_of_work(self, merkle_hash):
        """Generate a proof of work for the open transactions, the hash of the
        previous block and a random number (which is guessed until it fits)."""
        last_block = self.__chain[-1]
        last_hash = hash_block(last_block)
        proof = 0
        # Try different PoW numbers and return the first valid one
        while not Verification.valid_proof(
                merkle_hash, last_hash, proof):
            proof += 1
        return proof

    def get_balance(self, sender=None):
        """Calculate and return the balance for a participant.
        """
        if sender is None:
            if self.public_key is None:
                return None
            participant = self.public_key
        else:
            participant = sender
        # Fetch a list of all sent coin amounts for the given person (empty
        # lists are returned if the person was NOT the sender)
        # This fetches sent amounts of transactions that were already included
        # in blocks of the blockchain
        session = Session()
        tx_sender = session.query(Transaction.amount)\
            .filter(text('sender == :participant'))\
            .params(participant=str(participant)).all()
        session.close()

        # Fetch a list of all sent coin amounts for the given person (empty
        # lists are returned if the person was NOT the sender)
        # This fetches sent amounts of open transactions (to avoid double
        # spending)
        amount_sent = reduce(lambda tx_sum, tx_amt: tx_sum + sum(tx_amt)
        if len(tx_amt) > 0 else tx_sum + 0, tx_sender, 0)
        # This fetches received coin amounts of transactions that were already
        # included in blocks of the blockchain
        # We ignore open transactions here because you shouldn't be able to
        # spend coins before the transaction was confirmed + included in a
        # block
        session = Session()
        tx_recipient = session.query(Transaction.amount)\
            .filter(text('recipient == :participant AND mined == :mined'))\
            .params(participant=str(participant), mined=1).all()
        session.close()

        amount_received = reduce(
            lambda tx_sum, tx_amt: tx_sum + sum(tx_amt)
            if len(tx_amt) > 0 else tx_sum + 0,
            tx_recipient,
            0
        )
        # Return the total balance
        return amount_received - amount_sent

    def get_last_blockchain_value(self):
        """ Returns the last value of the current blockchain. """
        if len(self.__chain) < 1:
            return None
        return self.__chain[-1]

    def get_all_transactions(self):
        """Returns all transactions of the local blockchain"""
        session = Session()
        all_transactions = session.query(Transaction).all()
        session.close()

        sendable_tx = self.make_sendable_list(all_transactions)
        return sendable_tx

    # This function accepts two arguments.
    # One required one (transaction_amount) and one optional one
    # (last_transaction)
    # The optional one is optional because it has a default value => [1]

    def add_transaction(self,
                        recipient,
                        sender,
                        signature,
                        amount=1.0,
                        time=0,
                        is_receiving=False):
        """ Append a new value as well as the last blockchain value to the blockchain.

        Arguments:
            :sender: The sender of the coins.
            :recipient: The recipient of the coins.
            :signature: The signature of the transaction.
            :amount: The amount of coins sent with the transaction
            :time: The time when the transaction was made (generated with time())
            (default = 1.0)
        """
        session = Session()
        transaction = Transaction(sender, recipient, signature, amount, timed=time)
        session.add(transaction)
        if Verification.verify_transaction(transaction, self.get_balance):
            session.commit()
            session.close()
            self.load_data()

            if not is_receiving:
                for node in self.__peer_nodes:
                    url = 'http://{}/broadcast-transaction'.format(node['id'])
                    try:
                        response = requests.post(url,
                                                 json={
                                                     'sender': sender,
                                                     'recipient': recipient,
                                                     'amount': amount,
                                                     'signature': signature,
                                                     'time': time
                                                 })
                        if (response.status_code == 400 or
                                response.status_code == 500):
                            print('Transaction declined, needs resolving')
                            return False
                    except requests.exceptions.ConnectionError:
                        continue
            return True
        return False

    def mine_block(self):
        """Create a new block and add open transactions to it."""
        # Fetch the currently last block of the blockchain
        if self.public_key is None:
            return None
        last_block = self.__chain[-1]
        # Hash the last block (=> to be able to compare it to the stored hash
        # value)
        block_index = int(len(self.__chain))
        print(block_index)
        self.load_data()
        reward_transaction = Transaction(
            'MINING', str(self.public_key),
            'REWARD FOR MINING BLOCK {}'.format(block_index),
            MINING_REWARD, 1, block_index, time())
        print("rwd tx: ", reward_transaction.block)
        # Copy transaction instead of manipulating the original
        # open_transactions list
        # This ensures that if for some reason the mining should fail,
        # we don't have the reward transaction stored in the open transactions
        copied_transactions = self.__open_transactions[:]
        for tx in copied_transactions:
            if not Wallet.verify_transaction(tx):
                return None

        copied_transactions.append(reward_transaction)

        # add and modify the objects in the database
        hashed_transactions = Transaction.to_merkle_tree(copied_transactions)
        hashed_block = hash_block(last_block)
        proof = self.proof_of_work(hashed_transactions)
        session = Session()
        block = Block(block_index, hashed_block,
                      hashed_transactions, proof)
        session.add(block)
        session.add(reward_transaction)
        session.commit()
        open_txs = session.query(Transaction).filter(Transaction.mined == 0).all()

        for tx in open_txs:
            tx.block = block_index
            tx.mined = 1

        session.commit()
        session.close()

        self.load_data()

        session = Session()
        converted_block = session.query(Block)\
            .filter(Block.index == block_index).one()
        mined_transactions = session.query(Transaction).\
            filter(Transaction.block == block_index).all()
        session.close()

        sendable_tx = self.make_sendable_list(mined_transactions)

        dict_block = converted_block.__dict__.copy()
        del dict_block['_sa_instance_state']

        for node in self.__peer_nodes:
            url = 'http://{}/broadcast-block'.format(node['id'])
            try:
                response = requests.post(url, json={'block': dict_block,
                                                    'transactions': sendable_tx})
                if response.status_code == 400 or response.status_code == 500:
                    print('Block declined, needs resolving')
                if response.status_code == 409:
                    self.resolve_conflicts = True
            except requests.exceptions.ConnectionError:
                continue
        return block

    def add_block(self, block, list_of_transactions):
        """Add a block which was received via broadcasting to the local
        blockchain."""

        # Validate the proof of work of the block and store the result (True
        # or False) in a variable
        proof_is_valid = Verification.valid_proof(
            block['hash_of_txs'], block['previous_hash'], block['proof'])

        # Check if previous_hash stored in the block is equal to the local
        # blockchain's last block's hash and store the result in a block
        last_block = self.__chain[-1]
        index_of_block = block['index']
        hashes_match = hash_block(last_block) == block['previous_hash']
        if not proof_is_valid or not hashes_match:
            return False

        # Create a Block object
        session = Session()
        converted_block = Block(
            index_of_block,
            block['previous_hash'],
            block['hash_of_txs'],
            block['proof'],
            block['timestamp'])
        session.add(converted_block)

        # create a Transaction object of the given mining reward transactions
        # since this Transaction is not broadcasted
        reward_transaction = list_of_transactions[-1]
        reward_tx = Transaction(
            reward_transaction['sender'],
            reward_transaction['recipient'],
            reward_transaction['signature'],
            reward_transaction['amount'],
            1, index_of_block,
            reward_transaction['time'])
        session.add(reward_tx)

        session.commit()
        session.close()
        self.load_data()

        # Check which open transactions were included in the received block
        # and update the mined and block columns (except for the last transaction
        # in the list, that is the mining reward)
        mined_transactions = []
        session = Session()
        for itx in list_of_transactions[:-1]:
            try:
                for opentx in session.query(Transaction).filter(text("signature = :sign"))\
                        .params(sign=itx['signature']).all():
                            mined_transactions.append(opentx)
            except NoResultFound:
                continue

        for tx in mined_transactions:
            tx.block = index_of_block
            tx.mined = 1

        session.commit()
        session.close()

        self.load_data()
        return True

    def resolve(self):
        """Checks all peer nodes' blockchains and replaces the local one with
        longer valid ones."""
        # Initialize the winner chain with the local chain
        winner_chain = self.chain
        winning_node = self.node_id
        replace = False
        for node in self.__peer_nodes:
            url = 'http://{}/chain'.format(node['id'])
            try:
                # Send a request and store the response
                response = requests.get(url)
                # Retrieve the JSON data as a dictionary
                peer_node_data = response.json()
                node_chain = peer_node_data['chain']
                # Convert the dictionary list to a list of block AND
                # transaction objects

                node_chain = [
                    Block(
                        block['index'],
                        block['previous_hash'],
                        block['hash_of_txs'],
                        block['proof'],
                        block['timestamp']) for block in node_chain
                ]
                node_chain_length = len(node_chain)
                local_chain_length = len(winner_chain)
                # Store the received chain as the current winner chain if it's
                # longer AND valid
                if (node_chain_length > local_chain_length and
                        Verification.verify_chain(node_chain)):
                    winner_chain = node_chain
                    winning_node = node['id']
                    replace = True
            except requests.exceptions.ConnectionError:
                continue
        self.resolve_conflicts = False
        # Replace the local chain with the winner chain
        if self.chain is not winner_chain and replace:
            # get transactions from winning chain to replace the local ones
            url = 'http://{}/gettransactions'.format(winning_node)
            response = requests.get(url)
            transactions_object = response.json()
            transactions = transactions_object['transactions']
            session = Session()
            # delete local content of databases
            session.query(Transaction).delete()
            session.query(Block).delete()
            try:
                session.commit()
            except NoResultFound as e:
                print(e)

            new_transactions = [
                Transaction(
                    tx['sender'],
                    tx['recipient'],
                    tx['signature'],
                    tx['amount'],
                    tx['mined'],
                    tx['block'],
                    tx['time']) for tx in transactions
            ]

            session.add_all(new_transactions)
            session.add_all(winner_chain)
            session.commit()

        self.load_data()
        return replace

    def add_peer_node(self, node):
        """Adds a new node to the peer node set.

        Arguments:
            :node: The node URL which should be added.
        """
        session = Session()
        new_peer_node = Node(node)
        session.add(new_peer_node)
        try:
            session.commit()
        except IntegrityError:
            return False
        session.close()
        self.load_data()
        return True

    def remove_peer_node(self, node):
        """Removes a node from the peer node set.

        Arguments:
            :node: The node URL which should be removed.
        """
        session = Session()
        obj = session.query(Node).filter(text("id == :node_id"))\
            .params(node_id=node).one()
        session.delete(obj)
        try:
            session.commit()
        except NoResultFound:
            return False
        session.close()
        self.load_data()
        return True

    def get_peer_nodes(self):
        """Return a list of all connected peer nodes."""
        return list(self.__peer_nodes)

    def get_own_node(self):
        """Return own node ID"""
        # self.add_peer_node(f"localhost:{self.node_id}")
        return self.node_id

    @staticmethod
    def make_sendable_list(list_of_objects):
        """Converts list of objects to list of dicts so that the list can be sent

        Arguments:
            :list_of_objects: A SQLAlchemy list of objects
        """
        dict_list = []
        for item in list_of_objects:
            dict_item = item.__dict__.copy()
            del dict_item['_sa_instance_state']
            dict_list.append(dict_item)

        return dict_list
