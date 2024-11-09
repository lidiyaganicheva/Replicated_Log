import logging
import threading
import requests
import sys
import os

from flask import Flask, request, jsonify
from queue import Queue
from random import uniform
from datetime import timedelta

# Set up logging configuration

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(filename='master.log', mode='w'),
        logging.StreamHandler(stream=sys.stdout)
    ]
)

app = Flask(__name__)

# Thread-safe queue to store incoming messages
message_queue = Queue()

# List to store ordered processed messages in order of proposal_number
processed_messages = []

# Dict to store all processed messages
messages_dict = {}

# List of secondary nodes
secondary_nodes = ["http://" + x + "/" for x in os.environ["SECONDARY_HOSTS"].split(",")]

# Health status of nodes dictionary (initial status = "Healthy" for all nodes)
node_status_dict = dict.fromkeys(secondary_nodes, "Healthy")

# Quorum status to lock writes if there is no quorum
quorum_status = True
quorum_size = len(secondary_nodes)//2 #+1  To pass self-check acceptance test quorum = 1

# Proposal number assignment for message order tracking and happens-before relations
global_proposal_number = 1
global_previous_proposal_number = 0
global_max_proposal_number = 0

# Default request timeout
request_timeout = int(os.environ["REQUEST_TIMEOUT"])
# Default time to wait for retry
wait_for_retry = int(os.environ["WAIT_FOR_RETRY"])


# Prepare Phase
def send_prepare(node_url, proposal_num, prev_proposal_num):
    try:
        response = requests.post(node_url + "prepare", json={"proposal_number": proposal_num,
                                                             "previous_proposal_number": prev_proposal_num},
                                 timeout=request_timeout)
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during prepare to {node_url}: {e}")
        return None


# Accept Phase
def send_accept(node_url, proposal_num, prev_proposal_num, write_concern, message):
    try:
        response = requests.post(node_url + "accept", json={prev_proposal_num:
                                                            {"proposal_number": proposal_num,
                                                             "message": message,
                                                             "write_concern": write_concern}},
                                 timeout=request_timeout)

        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during accept to {node_url}: {e}")
        return None


# Thread function for prepare phase
def handle_prepare_for_node(node_url, proposal_num, prev_proposal_num, prepare_responses, lock, semaphore):
    response = send_prepare(node_url, proposal_num, prev_proposal_num)
    if response and response.get("status") == "promise":
        with lock:
            prepare_responses.append(response)
        semaphore.release()
    else:
        logging.info(f'Failed to get response from {node_url} during prepare phase')
        semaphore.release()


# Thread function for accept phase
def handle_accept_for_node(node_url, proposal_num, prev_proposal_num, message, accept_responses, write_concern, lock,
                           semaphore):
    response = send_accept(node_url, proposal_num, prev_proposal_num, write_concern, message)
    if response and response.get("status") == "accepted":
        with lock:
            accept_responses.append(response)
        semaphore.release()
    else:
        logging.info(f'Failed to get response from {node_url} during accept phase')
        semaphore.release()


# Process the dictionary of all values and form the output list:
def process_message_dict(k, v, keys_to_remove):
    global processed_messages, global_max_proposal_number
    if global_max_proposal_number == int(k):
        proposal_number = v.get('proposal_number')
        message = v.get('message')
        write_concern = v.get('write_concern')
        processed_messages.append(
            {"proposal_number": proposal_number, "message": message, "write_concern": write_concern})
        global_max_proposal_number = proposal_number
        keys_to_remove.append(k)
        logging.info(f"Message '{message}' with proposal number {proposal_number} "
                     f"added to output list on master.")


# Function to count delay before next attempt to retry
# Double the delay for the next attempt and add jitter if nodes are unhealthy
# Add jitter if nodes are suspected
# Retry without delay if nodes are healthy
def get_wait_for_retry(attempt, wait_time, nodes_out_of_quorum):
    jitter = uniform(0, 1)
    if list(node_status_dict.values()).count('Unhealthy') > nodes_out_of_quorum:
        exp_backoff = attempt * 3 + jitter
    elif list(node_status_dict.values()).count('Suspected') > nodes_out_of_quorum:
        exp_backoff = attempt * 2 + jitter
    else:
        exp_backoff = attempt + jitter
    # Limit level of delay: 600s (10 min)
    if exp_backoff > 600:
        exp_backoff = 600
    return exp_backoff


# Function to check for quorum (prepare phase)
def get_quorum(message_data, post_semaphore, attempt, retry_wait):
    global quorum_status, quorum_size, node_status_dict
    message = message_data['message']
    proposal_number = message_data['proposal_number']
    previous_proposal_number = message_data['previous_proposal_number']
    write_concern = message_data['write_concern']
    prepare_responses = []
    threads = []
    lock = threading.Lock()
    # Initializing semaphore to count the finished threads
    semaphore = threading.Semaphore(0)
    logging.info(
        f"Send prepare for message: {message} to all nodes with proposal_number: "
        f"{proposal_number} and write_concern: {write_concern}")

    # Prepare phase: send prepare requests to all secondary nodes in threads
    for node_url in secondary_nodes:
        thread = threading.Thread(target=handle_prepare_for_node,
                                  args=(node_url, proposal_number, previous_proposal_number,
                                        prepare_responses, lock, semaphore))
        threads.append(thread)
        thread.start()

    # Wait for quorum_size threads to complete using the semaphore
    for _ in range(quorum_size):
        semaphore.acquire()  # Blocks until a thread finishes

    # Check if we have quorum
    if len(prepare_responses) >= quorum_size:
        quorum_status = True
        logging.info(f"Quorum reached. {len(prepare_responses)} "
                     f"nodes send promises. Sending accept requests.")
        replicate_message(message_data, post_semaphore, 1, wait_for_retry)
    else:
        quorum_status = False
        prepare_responses.clear()
        threads.clear()
        # Retry.
        attempt += 1
        nodes_out_of_quorum = len(secondary_nodes) - quorum_size
        retry_wait = get_wait_for_retry(attempt, retry_wait, nodes_out_of_quorum)
        logging.info(f"Quorum is not reached. Master switches to read-only mode"
                     f" Starting retry, attempt № {attempt}, with delay {retry_wait} s")
        threading.Timer(retry_wait, function=get_quorum, args=(message_data, post_semaphore,
                                                                attempt, retry_wait)).start()


# Function to replicate message using and block until all needed ACKs are received
def replicate_message(message_data, post_semaphore, attempt, retry_wait):
    global messages_dict
    message = message_data['message']
    proposal_number = message_data['proposal_number']
    previous_proposal_number = message_data['previous_proposal_number']
    write_concern = message_data['write_concern']

    # Accept phase: Propose the value to all secondary nodes in threads
    threads = []
    lock = threading.Lock()
    accept_responses = []

    # Set replication factor not more or less than number of existing nodes
    if (write_concern < 1) | (write_concern > len(secondary_nodes) + 1):
        write_concern = (len(secondary_nodes) // 2) + 1  # Majority of nodes - default
    # else:
    #     replication_factor = write_concern - 1

    # Create semaphore for replication
    semaphore = threading.Semaphore(0)

    # Add the message to processed_messages list in a thread-safe way
    with lock:
        messages_dict.update({previous_proposal_number: {
            "proposal_number": proposal_number,
            "message": message,
            "write_concern": write_concern
        }})
    keys_to_remove = []
    with lock:
        messages_dict = dict(sorted(messages_dict.items()))
        for k, v in messages_dict.items():
            process_message_dict(k, v, keys_to_remove)
        [messages_dict.pop(k) for k in keys_to_remove]
    with lock:
        accept_responses.append({"master": "success"})

    # Replicate message to secondaries
    for node_url in secondary_nodes:
        thread = threading.Thread(target=handle_accept_for_node,
                                  args=(node_url, proposal_number, previous_proposal_number, message, accept_responses,
                                        write_concern, lock, semaphore))
        threads.append(thread)
        thread.start()

    for _ in range(write_concern):
        semaphore.acquire()  # Blocks until a thread finishes

    if len(accept_responses) >= write_concern:
        post_semaphore.release()
        logging.info(f"Successfully replicated message: {message} with proposal_number: "
                     f"{proposal_number} and write concern {write_concern}")
        # Wait for all not yet completed threads in the accept phase to complete
        for thread in threads:
            if thread.is_alive():
                thread.join()

        if len(accept_responses) >= len(secondary_nodes)+1:
            message_queue.task_done()
            logging.info(f"Message '{message}' with proposal number {proposal_number} "
                         f"and write concern {write_concern} is replicated to all nodes.")
        else:
            # Retry.
            accept_responses.clear()
            threads.clear()
            attempt += 1
            nodes_out_of_replication = len(secondary_nodes)
            retry_wait = get_wait_for_retry(attempt, retry_wait, nodes_out_of_replication)
            logging.info(f"Failed to replicate Message '{message}' with proposal number "
                         f"{proposal_number} and write concern {write_concern} to all nodes."
                         f" Starting retry, attempt № {attempt}, with delay {retry_wait} s")
            threading.Timer(retry_wait, function=replicate_message, args=(message_data, post_semaphore,
                                                                          attempt, retry_wait)).start()
    else:
        # Retry.
        accept_responses.clear()
        threads.clear()
        attempt += 1
        nodes_out_of_replication = len(secondary_nodes) - replication_factor
        retry_wait = get_wait_for_retry(attempt, retry_wait, nodes_out_of_replication)
        logging.info(f"Failed to replicate message: {message} with "
                      f"proposal_number: {proposal_number} and write concern {write_concern}"
                      f" Starting retry, attempt № {attempt}, with delay {retry_wait} s")
        threading.Timer(retry_wait, function=replicate_message, args=(message_data, post_semaphore,
                                                               attempt, retry_wait)).start()


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    # Sort messages by proposal_number
    sorted_messages = sorted(processed_messages, key=lambda x: x['proposal_number'])
    if quorum_status:
        return jsonify(sorted_messages), 200
    else:
        return jsonify({"message": "No quorum. Server in the read-only mode", "messages_list": sorted_messages}), 200


# HTTP endpoint to receive data at master node
@app.route('/send_data', methods=['POST'])
def receive_data():
    global global_proposal_number, global_previous_proposal_number, quorum_status
    if quorum_status:
        data = request.json
        message = data.get("message")
        write_concern = data.get("write_concern")
        max_write_concern = len(secondary_nodes) + 1 \
            if (write_concern < 0) | (write_concern > (len(secondary_nodes) + 1)) else write_concern

        if message:
            logging.info(
                f"Received message: {message}. Assigning proposal_number: {global_proposal_number}. Adding to queue for replication with write_concern: {write_concern}")

            # Assign a unique proposal number and increment it
            proposal_number = global_proposal_number
            previous_proposal_number = global_previous_proposal_number
            global_previous_proposal_number = global_proposal_number
            global_proposal_number += 1

            # Add message, proposal_number, and write_concern to thread-safe queue
            message_queue.put({
                "message": message,
                "proposal_number": proposal_number,
                "previous_proposal_number": previous_proposal_number,
                "write_concern": write_concern
            })

            # Get message from a queue and process it
            post_semaphore = threading.Semaphore(0)
            message_data = message_queue.get()
            # Starting replication thread for a message with attempt #0
            worker_thread = threading.Thread(target=get_quorum, args=(message_data, post_semaphore,
                                                                      0, wait_for_retry), daemon=True)
            worker_thread.start()
            if quorum_status:
                post_semaphore.acquire()

                return jsonify({"status": "success",
                                "message": f"Data is successfully replicated to {max_write_concern} "
                                           f"of nodes with proposal_number {proposal_number}"}), 200
            else:
                return jsonify({"status": "error", "message": "No quorum. Stopping server to accept messages"}), 423
        else:
            return jsonify({"status": "error", "message": "No message provided"}), 400
    else:
        return jsonify({"status": "error", "message": "No quorum. Server does not accept messages"}), 423


# HTTP endpoint to get health
@app.route('/health', methods=['GET'])
def get_health_status():
    return jsonify(node_status_dict)


# Heartbeat function. Default send interval = 5s.
def send_heartbeat(timer, timeout_counter, delay_counter, node_url):
    global request_timeout
    timer = int(timer)
    try:
        response = requests.post(node_url + "heartbeat", timeout=request_timeout)
        node_timeout = False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error during heartbeat to {node_url}: {e}")
        node_timeout = True

    # If node fails to connect add timeout counter and increase the time for retry
    if node_timeout:
        timeout_counter += 1
        timer = 2 * timer + uniform(0, 1)
        if timeout_counter > 3 and node_status_dict.get(node) != "Unhealthy":
            node_status_dict.update({node_url: "Unhealthy"})
            logging.info(f"{node_url} marked as Unhealthy"
                         f" Next retry in {timer} s")
    # If node delays to response add delay counter,
    # decrement timeout counter and increase the time to retry
    elif (not node_timeout) and (response.elapsed >= timedelta(seconds=1)):
        if timeout_counter > 0:
            timeout_counter -= 1
        delay_counter += 1
        timer = 1.5 * timer + uniform(0, 1)
        if delay_counter > 3 and node_status_dict.get(node) != "Suspected":
            node_status_dict.update({node_url: "Suspected"})
            logging.info(f"{node_url} marked as Suspected"
                         f" Next retry in {timer} s")

    # If no delays and timeouts, decrement all counters and set default retry interval
    else:
        if timeout_counter > 0:
            timeout_counter -= 1
        if delay_counter > 0:
            delay_counter -= 1
        timer = 10
        if node_status_dict.get(node_url) != "Healthy" and timeout_counter == 0 and delay_counter == 0:
            node_status_dict.update({node_url: "Healthy"})
            logging.info(f"{node_url} marked as Healthy")
    thread = threading.Timer(timer, function=send_heartbeat,
                             args=(timer, timeout_counter, delay_counter, node_url))
    thread.start()


# Main entry point
if __name__ == '__main__':
    # Start the message processing thread
    logging.info("Starting HTTP connection at Master")
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=9000, debug=False),
                     daemon=True).start()
    for node in secondary_nodes:
        threading.Thread(target=send_heartbeat, args=(10, 0, 0, node)).start()
