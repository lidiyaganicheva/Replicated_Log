from flask import Flask, request, jsonify
import os
import sys
import logging
from time import sleep
from random import randrange

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
accepted_message = None
messages_list = []
proposal_numbers = []


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    return jsonify(messages_list), 200


# Handle prepare requests
@app.route('/prepare', methods=['POST'])
def handle_prepare():
    global proposal_numbers
    data = request.json
    if node_timeout:
        sleep(20)
    proposal_number = data.get('proposal_number')

    if proposal_number not in proposal_numbers:
        proposal_numbers.append(proposal_number)
        return jsonify({"status": "promise", "proposal_number": proposal_number})
    else:
        return jsonify({"status": "reject", "proposal_number": proposal_number})


# Handle accept requests
@app.route('/accept', methods=['POST'])
def handle_accept():
    global accepted_message, messages_list
    data = request.json
    if node_timeout:
        logging.info(f'{container_name} is sleeping')
        sleep(20)
    proposal_number = data.get('proposal_number')
    message = data.get('message')
    write_concern = data.get('write_concern')

    if isinstance(proposal_number, int):
        accepted_message = message
        messages_list.append({"proposal_number": proposal_number, "message": message, "write_concern": write_concern})
        # Deduplication
        messages_list = list(map(dict, set(tuple(dct.items()) for dct in messages_list)))
        # Ordering
        messages_list = sorted(messages_list, key=lambda x: x["proposal_number"])

        return jsonify({"status": "accepted", "message": accepted_message})
    else:
        return jsonify({"status": "reject", "proposal_number": proposal_number})


# Main entry point
if __name__ == '__main__':
    logging.info(f"Starting HTTP connection at {container_name}")
    app.run(host='0.0.0.0', port=port_number, debug=False)
