# Replicated_Log
### Build an image:
docker image build -t python-app .
### Run the project:
docker compose -f "iteration_1\docker-compose.yml" up

### Send the POST request to Master node:
curl -X POST "http://localhost:9000/send_data" -H "Content-Type: application/json" -d "{\"message\": \"value\"}"

### Send GET request to Master and Secondaries:
http://localhost:9000/
http://localhost:9001/
http://localhost:9002/

### Application logic:
Replicated Log application was implemented using Paxos protocol.
#### Roles:
Proposers: Suggest values to be agreed upon.
Acceptors: Vote on proposed values and ensure a majority consensus.
(In this case consensus ensured when message replicated in all secondaries
according to the task)
Learners: Learn the chosen value after consensus is reached.
#### Phases:
Prepare Phase: A proposer sends a prepare request with a unique proposal number 
to a majority of acceptors. Acceptors respond with a promise not to accept proposals 
with lower numbers and may include the last accepted proposal.
Promise Phase: If an acceptor receives a prepare request with a higher number 
than any previously seen, it promises not to accept any lower-numbered proposals.
Accept Phase: Upon receiving promises, the proposer sends an accept request with 
the proposal number and value to the acceptors. Acceptors accept the proposal 
if it matches their promise.
Learn Phase: Once a value is accepted by a majority, it is communicated to the 
learners as the chosen value.
Quorum: A majority of acceptors must agree on a proposal for it to be chosen, 
ensuring consistency.
Fault Tolerance: Paxos tolerates failures of some nodes, as long as a majority 
of acceptors remain operational, maintaining system reliability.


