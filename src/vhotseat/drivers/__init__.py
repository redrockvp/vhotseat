"""How vhotseat talks to each kind of workload.

A driver answers the arbiter's questions about one workload (is it loaded? is
it busy?) and performs the one action the arbiter needs from it: releasing its
memory without interrupting work. See base.Workload for the contract.
"""
