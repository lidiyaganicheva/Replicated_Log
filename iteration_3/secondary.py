from flask import Flask, request, jsonify
import os
import sys
import logging
from time import sleep
from random import randrange, choices
from threading import Lock

# Set up logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(filename=f'{os.environ["CONT_NAME"]}.log', mode='w'),
        logging.StreamHandler(stream=sys.stdout)
    ]
)

app = Flask(__name__)

# Get environment variables from docker-compose to set up a secondary
port_number = os.environ["PORT_NUMBER"]
node_timeout = True if os.environ["TIMEOUT"] == "true" else False
container_name = os.environ["CONT_NAME"]

# Set up sleep time for node here to emulate network issues
max_sleep_time = 6

# Acceptor state
accepted_proposal_number = 0
# list of all valid messages for output:
processed_messages = []
# dictionary of all received messages:
messages_dict = {}
global_previous_proposal_number = 0


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    # sorted_messages = sorted(messages_list, key=lambda x: x['proposal_number'])
    return jsonify(processed_messages), 200


# Handle prepare requests
@app.route('/prepare', methods=['POST'])
def handle_prepare():
    global accepted_proposal_number
    data = request.json
    if node_timeout:
        sleep(randrange(max_sleep_time))
    proposal_number = data.get('proposal_number')
    previous_proposal_number = data.get('previous_proposal_number')

    if previous_proposal_number < proposal_number:
        return jsonify({"status": "promise", "proposal_number": proposal_number})
    else:
        return jsonify({"status": "reject", "proposal_number": proposal_number})


# Process the dictionary of all values and form the output list:
def process_message_dict(k, v, keys_to_remove):
    global processed_messages, global_previous_proposal_number, accepted_proposal_number
    if global_previous_proposal_number == int(k):
        proposal_number = v.get('proposal_number')
        message = v.get('message')
        write_concern = v.get('write_concern')
        processed_messages.append(
            {"proposal_number": proposal_number, "message": message, "write_concern": write_concern})
        global_previous_proposal_number = proposal_number
        keys_to_remove.append(k)


# Handle accept requests
@app.route('/accept', methods=['POST'])
def handle_accept():
    global messages_dict
    data = request.json
    lock = Lock()
    if node_timeout:
        sleep(randrange(max_sleep_time))
    proposal_number = data.get([*data][0]).get("proposal_number")
    message = data.get([*data][0]).get("message")
    if data:
        with lock:
            messages_dict.update(data)
            keys_to_remove = []
            messages_dict = dict(sorted(messages_dict.items()))
            for k, v in messages_dict.items():
                process_message_dict(k, v, keys_to_remove)
            [messages_dict.pop(k) for k in keys_to_remove]
        # Generate random accept or reject status with weight 0.7 for accept
        # and 0.3 for reject
        return_list = [{"status": "accepted", "proposal_number": proposal_number},
                       {"status": "reject", "proposal_number": proposal_number}]
        return_status = choices(population=return_list, weights=[0.7, 0.3], k=1)[0]
        if return_status.get("status") == "reject":
            logging.info(f'Sending false reject to master for message {message}'
                         f' with proposal number {proposal_number}')
        return jsonify(return_status)
    else:
        return jsonify({"status": "reject", "proposal_number": proposal_number})


# Handle heartbeats
@app.route('/heartbeat', methods=['POST'])
def handle_heartbeat():
    if node_timeout:
        sleep(randrange(max_sleep_time))
    return jsonify({"message": "Request received"}), 200


# Main entry point
if __name__ == '__main__':
    logging.info(f"Starting HTTP connection at {container_name}")
    app.run(host='0.0.0.0', port=port_number, debug=False)
