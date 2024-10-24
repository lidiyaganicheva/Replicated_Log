import logging
import threading
import requests
import sys
import os

from flask import Flask, request, jsonify
from queue import Queue

# Set up logging configuration

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(filename='master.log', mode='w'),
        logging.StreamHandler(stream=sys.stdout)
    ]
)

app = Flask(__name__)

# Thread-safe queue to store incoming messages
message_queue = Queue()

# List to store processed messages in order of proposal_number
processed_messages = []

secondary_nodes = ["http://" + x + "/" for x in os.environ["SECONDARY_HOSTS"].split(",")]

global_proposal_number = 1

# Maximum number of worker threads for parallel processing
MAX_WORKERS = 10


# Prepare Phase
def send_prepare(node_url, proposal_num):
    try:
        response = requests.post(node_url + "prepare", json={"proposal_number": proposal_num}, )
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during prepare to {node_url}: {e}")
        return None


# Accept Phase
def send_accept(node_url, proposal_num, write_concern, message):
    try:
        response = requests.post(node_url + "accept", json={"proposal_number": proposal_num, "message": message,
                                                            "write_concern": write_concern})
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during accept to {node_url}: {e}")
        return None


# Thread function for prepare phase
def handle_prepare_for_node(node_url, proposal_num, prepare_responses, lock, semaphore):
    response = send_prepare(node_url, proposal_num)
    if response and response.get("status") == "promise":
        with lock:
            prepare_responses.append(response)
        semaphore.release()


# Thread function for accept phase
def handle_accept_for_node(node_url, proposal_num, message, accept_responses, write_concern, lock, semaphore):
    response = send_accept(node_url, proposal_num, write_concern, message)
    if response and response.get("status") == "accepted":
        with lock:
            accept_responses.append(response)
        semaphore.release()


# Function to replicate message using Paxos and block until all ACKs are received
def replicate_message(message_data, post_semaphore):
    message = message_data['message']
    proposal_number = message_data['proposal_number']
    write_concern = message_data['write_concern']
    prepare_responses = []
    threads = []
    lock = threading.Lock()
    # Initializing semaphore to count the finished threads
    semaphore = threading.Semaphore(0)
    # Set write concern not more or less than number of existing nodes
    if (write_concern < 1) | (write_concern > len(secondary_nodes) + 1):
        quorum_size = (len(secondary_nodes) // 2) + 1  # Majority of nodes - default
    else:
        quorum_size = write_concern - 1

    logging.info(
        f"Processing message from queue: {message} with proposal_number: {proposal_number} and write_concern: {write_concern}")

    # Prepare phase: send prepare requests to all secondary nodes in threads
    for node in secondary_nodes:
        thread = threading.Thread(target=handle_prepare_for_node, args=(node, proposal_number, prepare_responses, lock, semaphore))
        threads.append(thread)
        thread.start()

    # Wait for quorum_size threads to complete using the semaphore
    for _ in range(quorum_size):
        semaphore.acquire()  # Blocks until a thread finishes

    # Check if we have quorum
    if len(prepare_responses) >= quorum_size:
        logging.info(f"Quorum reached. {len(prepare_responses)} nodes send promises. Sending accept requests.")

        # Accept phase: Propose the value to all secondary nodes in threads
        accept_responses = []
        threads = []

        # Create new semaphore for replication
        semaphore = threading.Semaphore(0)

        for node in secondary_nodes:
            thread = threading.Thread(target=handle_accept_for_node,
                                      args=(node, proposal_number, message, accept_responses, write_concern, lock, semaphore))
            threads.append(thread)
            thread.start()

        for _ in range(quorum_size):
            semaphore.acquire()  # Blocks until a thread finishes

        if len(accept_responses) >= quorum_size:
            # Add the message to processed_messages list in a thread-safe way
            with lock:
                processed_messages.append({
                    "proposal_number": proposal_number,
                    "message": message,
                    "write_concern": write_concern
                })
            post_semaphore.release()
            logging.info(f"Successfully replicated message: {message} with proposal_number: "
                         f"{proposal_number} and write concern {write_concern}")
            # Wait for all not yet completed threads in the accept phase to complete
            for thread in threads:
                if thread.is_alive():
                    thread.join()

            if len(accept_responses) == len(secondary_nodes):
                message_queue.task_done()
                logging.info(f"Message '{message}' with proposal number {proposal_number} "
                             f"and write concern {write_concern} is replicated to all nodes.")
            else:
                logging.info(f"Failed to replicate Message '{message}' with proposal number "
                             f"{proposal_number} and write concern {write_concern} to all nodes.")
        else:
            logging.error(f"Failed to replicate message: {message} with "
                          f"proposal_number: {proposal_number} and write concern {write_concern}")


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    # Sort messages by proposal_number
    sorted_messages = sorted(processed_messages, key=lambda x: x['proposal_number'])
    return jsonify(sorted_messages), 200


# HTTP endpoint to receive data at master node
@app.route('/send_data', methods=['POST'])
def receive_data():
    global global_proposal_number
    data = request.json
    message = data.get("message")
    write_concern = data.get("write_concern")
    max_write_concern = len(secondary_nodes)+1 \
        if (write_concern < 0) | (write_concern > (len(secondary_nodes)+1)) else write_concern

    if message:
        logging.info(
            f"Received message: {message}. Assigning proposal_number: {global_proposal_number}. Adding to queue for replication with write_concern: {write_concern}")

        # Assign a unique proposal number and increment it
        proposal_number = global_proposal_number
        global_proposal_number += 1

        # Add message, proposal_number, and write_concern to thread-safe queue
        message_queue.put({
            "message": message,
            "proposal_number": proposal_number,
            "write_concern": write_concern
        })

        # Get message from a queue and process it
        post_semaphore = threading.Semaphore(0)
        message_data = message_queue.get()
        worker_thread = threading.Thread(target=replicate_message, args=(message_data, post_semaphore), daemon=True)
        worker_thread.start()
        post_semaphore.acquire()

        return jsonify({"status": "success",
                        "message": f"Data is successfully replicated to {max_write_concern} "
                                   f"of nodes with proposal_number {proposal_number}"}), 200
    else:
        return jsonify({"status": "error", "message": "No message provided"}), 400


# Main entry point
if __name__ == '__main__':
    # Start the message processing thread
    logging.info("Starting HTTP connection at Master")
    app.run(host='0.0.0.0', port=9000, debug=False)
