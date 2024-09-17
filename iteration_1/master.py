import logging
import threading
import requests
import sys, os

from flask import Flask, request, jsonify

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
secondary_nodes = [
    "http://secondary_1:9001/",
    "http://secondary_2:9002/"
]

# Initial Proposer state

proposal_number = 1
quorum_size = len(secondary_nodes)  # according to the task
messages_list = []


# Prepare Phase
def send_prepare(node_url, proposal_num):
    try:
        response = requests.post(node_url + "prepare", json={"proposal_number": proposal_num}, )
        logging.info(f'Prepare execution time: {response.elapsed.total_seconds()} s')
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during prepare to {node_url}: {e}")
        return None


# Accept Phase
def send_accept(node_url, proposal_num, message):
    try:
        response = requests.post(node_url + "accept", json={"proposal_number": proposal_num, "message": message})
        logging.info(f'Accept execution time: {response.elapsed.total_seconds()} s')
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during accept to {node_url}: {e}")
        return None


# Thread function for prepare phase
def handle_prepare_for_node(node_url, proposal_number, prepare_responses, lock):
    response = send_prepare(node_url, proposal_number)
    if response and response.get("status") == "promise":
        with lock:
            prepare_responses.append(response)


# Thread function for accept phase
def handle_accept_for_node(node_url, proposal_num, message, accept_responses, lock):
    response = send_accept(node_url, proposal_num, message)
    if response and response.get("status") == "accepted":
        with lock:
            accept_responses.append(response)


# Function to replicate message using Paxos and block until all ACKs are received
def replicate_message(message):
    global proposal_number
    prepare_responses = []
    threads = []
    lock = threading.Lock()

    # Prepare phase: send prepare requests to all secondary nodes in threads
    for node in secondary_nodes:
        thread = threading.Thread(target=handle_prepare_for_node, args=(node, proposal_number, prepare_responses, lock))
        threads.append(thread)
        thread.start()

    # Wait for all threads in the prepare phase to complete
    for thread in threads:
        thread.join()

    # Check if we have quorum
    if len(prepare_responses) >= quorum_size:
        logging.info("Quorum reached. Sending accept requests.")

        # Accept phase: Propose the value to all secondary nodes in threads
        accept_responses = []
        for node in secondary_nodes:
            thread = threading.Thread(target=handle_accept_for_node,
                                      args=(node, proposal_number, message, accept_responses, lock))
            threads.append(thread)
            thread.start()

        # Wait for all threads in the accept phase to complete
        for thread in threads:
            thread.join()

        # If all nodes accepted the proposal, return success
        if len(accept_responses) == len(secondary_nodes):
            logging.info(f"Message '{message}' successfully replicated with proposal number {proposal_number}.")
            proposal_number += 1
            return True
        else:
            logging.error("Failed to get ACK from all secondary nodes during the accept phase.")
            proposal_number += 1
            return False
    else:
        logging.error("Failed to reach quorum in the prepare phase.")
        proposal_number += 1
        return False

    proposal_number += 1
    logging.info(f'Proposal number for the next message:{proposal_number}')


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    return f'Ordered messages: {messages_list}', 200


# HTTP endpoint to receive data at master node
@app.route('/send_data', methods=['POST'])
def receive_data():
    data = request.json
    message = data.get("message")

    if message:
        logging.info(f"Received message: {message}. Starting replication.")
        success = replicate_message(message)

        if success:
            messages_list.append(message)
            return jsonify({"status": "success", "message": "Data replicated to all secondaries"}), 200
        else:
            return jsonify({"status": "error", "message": "Failed to replicate data"}), 500
    else:
        return jsonify({"status": "error", "message": "No message provided"}), 400


# Main entry point
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9000, debug=True)
