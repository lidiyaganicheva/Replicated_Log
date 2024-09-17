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
node_timeout = bool(os.environ["TIMEOUT"])
# Set up sleep time for node here to emulate network issues
max_sleep_time = 6

# Acceptor state
highest_proposal_number = 0
accepted_message = None
messages_list = []


# Route for handling GET requests
@app.route('/', methods=['GET'])
def handle_get():
    return f'Ordered messages: {messages_list}', 200


# Handle prepare requests
@app.route('/prepare', methods=['POST'])
def handle_prepare():
    global highest_proposal_number
    data = request.json
    if node_timeout:
        sleep(randrange(max_sleep_time))
    proposal_number = data.get('proposal_number')

    if proposal_number > highest_proposal_number:
        highest_proposal_number = proposal_number
        return jsonify({"status": "promise", "proposal_number": highest_proposal_number})
    else:
        return jsonify({"status": "reject", "proposal_number": highest_proposal_number})


# Handle accept requests
@app.route('/accept', methods=['POST'])
def handle_accept():
    global highest_proposal_number, accepted_message, messages_list
    data = request.json
    if node_timeout:
        sleep(randrange(max_sleep_time))
    proposal_number = data.get('proposal_number')
    message = data.get('message')

    if proposal_number >= highest_proposal_number:
        highest_proposal_number = proposal_number
        accepted_message = message
        messages_list.append(message)

        return jsonify({"status": "accepted", "message": accepted_message})
    else:
        return jsonify({"status": "reject", "proposal_number": highest_proposal_number})


# Main entry point
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=port_number, debug=True)
