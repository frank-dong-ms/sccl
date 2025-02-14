# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from dataclasses import dataclass
from enum import Enum
import heapq

from sccl.language.ir import *
from sccl.language.passes import *

# Returns whether an operation writes to a particular slot
def writes_to_slot(op, slot):
    # If the instruction is a copy or reduce, check to see if the destination matches the slot
    if op.inst == Instruction.copy or op.inst == Instruction.reduce:
        cpy_src = op.src
        _, buffer, index = slot
        return buffer != cpy_src.buffer or (index < cpy_src.index and index > (cpy_src.index + cpy_src.size))
    return op.inst != Instruction.send

def remove_op(op):
    for p in op.prev:
        p.next.remove(op)
        p.next += op.next

    for n in op.next:
        n.prev.remove(op)
        n.prev =  op.prev.union(n.prev)

def same_tb(op1, op2):
    return op1.tb == op2.tb

def same_count(op1, op2):
    return op1.cnt() == op2.cnt()
    
def same_buf_dst(op1, op2):
    return op1.dst.buffer == op2.dst.buffer and op1.dst.index == op2.dst.index

class RankDAG:
    def __init__(self, num_ranks, buffers):
        self.num_ranks = num_ranks
        self.buffers = buffers
        self.slots = [] # slot = (rank, buffer, index)
        self.operations = {} # slot -> operations
        self.tbs = [] 
        for _ in range(num_ranks):
            self.tbs.append({}) 
        self.tb_mapping = {}


    def add_start(self, rank, buffer, index, ref):
        slot = (rank, buffer, index)
        self.slots.append(slot)

        op = Op(Instruction.start, rank, ref, ref, next=set(), prev=set())
        self.operations[slot] = op

    # Find the last write to happen on this slot
    def find_last_recv(self, slot):
        def dfs(op):
            # Found the last operation on the slot
            if len(op.next) == 0:
                return writes_to_slot(op, slot), op
            else:
                last_recvs = False
                # Check if any of the children is the last write
                for o in op.next:
                    is_last_recv, recv_op = dfs(o)
                    if is_last_recv:
                        return True, recv_op
                    last_recvs = last_recvs or is_last_recv
                # Check if we are the last write
                if writes_to_slot(op, slot) and not last_recvs:
                    return True, op
                return False, op
        
        result, op = dfs(self.operations[slot])
        assert result
        return op

    # Find the last set of operations that happened on this slot
    # There may be multiple as sends can happen in parallel
    def find_last_ops(self, slot):
        frontier = [self.operations[slot]]
        last_ops = []
        while len(frontier) > 0:
            op = frontier[0]
            if len(op.next) == 0:
                last_ops.append(op)
            frontier = frontier[1:] + list(op.next)   
        return last_ops

    def add_copy(self, rank, send_ref, recv_ref, step, priority, tb):
        op = Op(Instruction.copy, rank, send_ref, recv_ref, chunk_step=step, priority=priority, next=set(), prev=set(), tb=tb)
        dstbuffer = recv_ref.buffer
        dstindex = recv_ref.index
        srcbuffer = send_ref.buffer
        srcindex = send_ref.index
        size = recv_ref.size
        prev_ops = []

        # Sending part of copy
        for i in range(srcindex, srcindex+size):
            slot = (rank, srcbuffer, i)
            prev_op = self.find_last_recv(slot) # All operations that need to happen before
            prev_op.next.add(op)
            op.prev.add(prev_op)

        # Receiving part of copy
        prev_ops = set()
        for i in range(dstindex, dstindex+size):
            slot = (rank, dstbuffer, i)
            if slot in self.operations:
                prev_op = self.find_last_ops(slot)
                prev_ops.append(prev_op) # All operations that need to happen before
            else:
                self.operations[slot] = op

        for prev_op in prev_ops:
            if op not in prev_op.next:
                prev_op.next.add(op)
                op.prev.add(prev_op)

    def add_reduce(self, rank, send_ref, recv_ref, step, priority, tb):
        op = Op(Instruction.reduce, rank, send_ref, recv_ref, chunk_step=step, priority=priority, next=set(), prev=set(), tb=tb)
        dstbuffer = recv_ref.buffer
        dstindex = recv_ref.index
        srcbuffer = send_ref.buffer
        srcindex = send_ref.index
        size = recv_ref.size
        prev_ops = []

        # B
        for i in range(srcindex, srcindex+size):
            slot = (rank, srcbuffer, i)
            prev_op = self.find_last_recv(slot) # All operations that need to happen before
            prev_op.next.add(op)
            op.prev.add(prev_op)

        # A
        prev_ops = []
        for i in range(dstindex, dstindex+size):
            slot = (rank, dstbuffer, i)
            if slot in self.operations:
                prev_op = self.find_last_ops(slot)
                prev_ops = prev_ops + prev_op # All operations that need to happen before
       
        for prev_op in prev_ops:
            if op not in prev_op.next:
                prev_op.next.add(op)
                op.prev.add(prev_op)

    def add_send(self, rank, send_ref, recv_ref, step, priority, tb, ch):
        op = Op(Instruction.send, rank, send_ref, recv_ref, chunk_step=step, priority=priority, next=set(), prev=set(), tb=tb, channel=ch)
        buffer = send_ref.buffer
        index = send_ref.index
        size = send_ref.size
        prev_ops = []
        for i in range(index, index+size):
            slot = (rank, buffer, i)
            prev_op = self.find_last_recv(slot)
            prev_ops.append(prev_op) # All operations that need to happen before

        for prev_op in prev_ops:
            if op not in prev_op.next:
                prev_op.next.add(op)
                op.prev.add(prev_op)
        return op

    def add_recv(self, rank, send_ref, recv_ref, step, priority, tb, ch):
        op = Op(Instruction.recv, rank, send_ref, recv_ref, chunk_step=step, priority=priority, next=set(), prev=set(), tb=tb, channel=ch)
        buffer = recv_ref.buffer
        index = recv_ref.index
        size = recv_ref.size

        prev_ops = set()
        for i in range(index, index+size):
            slot = (rank, buffer, i)
            if slot in self.operations:
                slot_prev_ops = self.find_last_ops(slot) # All operations that need to happen before
                prev_ops = prev_ops.union(slot_prev_ops)        
            else:
                self.operations[slot] = op
        if len(prev_ops) > 0:
                for prev_op in prev_ops:
                    prev_op.next.add(op)
                    op.prev.add(prev_op)
        return op

    def add_recv_reduce_copy(self, rank, send_ref, recv_ref, step, priority, tb, ch):
        op = Op(Instruction.recv_reduce_copy, rank, send_ref, recv_ref, chunk_step=step, priority=priority, next=set(), prev=set(), tb=tb, channel=ch)
        buffer = recv_ref.buffer
        index = recv_ref.index
        size = recv_ref.size

        prev_ops = set()
        for i in range(index, index+size):
            slot = (rank, buffer, i)
            if slot in self.operations:
                slot_prev_ops = self.find_last_ops(slot) # All operations that need to happen before
                prev_ops = prev_ops.union(slot_prev_ops)        
            else:
                self.operations[slot] = op
        if len(prev_ops) > 0:
                for prev_op in prev_ops:
                    prev_op.next.add(op)
                    op.prev.add(prev_op)
        return op

    def convert_set_list(self):
        ops = []
        for slot, op in self.operations.items():
            if op.inst == Instruction.start:
                op.next = list(op.next)
                for o in op.next:
                    ops.append(o)
            elif op.inst != Instruction.copy:
                ops.append(op)

            visited = set()
            while len(ops) > 0:
                op = ops[0]
                if op not in visited:
                    visited.add(op)
                    op.next = list(op.next)
                    ops = ops[1:] + op.next
                else:
                    ops = ops[1:]
                    
    def optimize(self):
        self._optimize_rrcs_rrs()
        self._optimize_rcs()
        
    # Given the set of operations that operate over a particular slot (rank, buffer, idx) fixed
    # Try and replace operations with pipelined ops like receive copy send (rcs)
    # or receive reduce send (rrs) and receive reduce copy send (rrcs)
    # Rules:
    # recv-copy-send 
    # recv(src, sbuf, si, _, _, _ ) send(_, _, _, dst, dbuf, di) -> recv_copy_send(src, sbuf, si, dst, dbuf, di)
    def _optimize_rcs(self):
        for slot, ops in self.operations.items():
            frontier = [ops]
            while len(frontier) > 0:
                op = frontier[0]
                if len(op.next) == 1:
                    next_op = op.next[0] 
                    if op.inst == Instruction.recv and next_op.inst == Instruction.send and same_tb(op, next_op) and same_count(op, next_op) and same_buf_dst(op, next_op):
                        op.inst = Instruction.recv_copy_send
                        op.dst = next_op.dst
                        op.match = op.match + next_op.match
                        remove_op(next_op)
                frontier = frontier[1:] + op.next
        
    def _optimize_rrcs_rrs(self):
        # RRC/S -> RRS
        for slot, ops in self.operations.items():
            frontier = [ops]
            while len(frontier) > 0:
                op = frontier[0]
                if len(op.next) == 1:
                    next_op = op.next[0]
                    if len(next_op.next) == 1:
                        nnext_op = next_op.next[0]
                        if op.inst == Instruction.recv_reduce_copy and next_op.inst == Instruction.send and nnext_op.inst == Instruction.recv and same_tb(op, next_op) and same_count(op, next_op):
                            op.inst = Instruction.recv_reduce_send
                            op.dst = next_op.dst
                            op.match = op.match + next_op.match
                            remove_op(next_op)
                    
                    if op.inst == Instruction.recv_reduce_copy and next_op.inst == Instruction.send and same_tb(op, next_op) and same_count(op, next_op):
                        op.inst = Instruction.recv_reduce_copy_send
                        op.dst = next_op.dst
                        op.match = op.match + next_op.match
                        remove_op(next_op)
                frontier = frontier[1:] + op.next

    def lower_pt1(self, instances):
        self.infer_dependencies()
        self.lower_buffers(instances)
    
    def lower_pt2(self, instances, interleaved):
        self.replicate(instances, interleaved)
        return self.lower_tbs()


    def infer_dependencies(self):
        for slot, ops in self.operations.items():
            frontier = [ops]
            while len(frontier) > 0:
                op = frontier[0]
                # Dependencies for every op is the same as the ops that are stored in prev
                # Filter out dependencies that are satisified by tbs executing ops sequentially
                # If multiple dependent ops from the same tb keep the one that happens last
                depends = {}
                for dep_op in list(op.prev):
                    if dep_op.inst != Instruction.start:
                        tb = dep_op.tb
                        if tb not in depends or dep_op.step > depends[tb].step:
                            depends[tb] = dep_op
                op.depends = list(depends.values())
                frontier = frontier[1:] + op.next

    # Convert local scratch buffers to index into one global scratch buffer
    def lower_chunk(self, chunk):
        if chunk.buffer is not Buffer.input and chunk.buffer is not Buffer.output:
            buffer = self.buffers[chunk.rank][chunk.buffer].get_buffer()
            index = self.buffers[chunk.rank][chunk.buffer].get_global_index(chunk.index)
            return ChunkRef(chunk.rank, buffer, index, chunk.size)
        return chunk

    # Assigns each scratch buffer an offset into the global scratch buffer
    def lower_buffers(self, instances):
        for rank_buffers in self.buffers:
            offset = 0
            for key, buf in rank_buffers.items():
                if key is not Buffer.input and key is not Buffer.output:
                    buf.set_offset(offset)
                    offset += buf.instance_size() * instances

    # Preprocess the threadblocks for lowering into xml
    def lower_tbs(self):
        gpus = []
        for rank, rank_tbs in enumerate(self.instanced_tbs):
            lowered_tbs = {}
            for tbid, tb in rank_tbs.items():
                for op in tb.ops:
                    op.src = self.lower_chunk(op.src)
                    op.dst = self.lower_chunk(op.dst)
                lowered_tbs[tbid] = tb
            gpus.append(Gpu(rank, list(lowered_tbs.values())))
        return gpus


    # Automatically replicates the algorithm instance number of times
    # interleaved sets the replication policy
    # if True chunks are split as: ChunkA ChunkB -> ChunkA0 ChunkA1 .. ChunkB0 ChunkB1 ...
    # if false chunks are divided as ChunkA0 ChunkB0 ChunkA1 ChunkB1 ...
    # For collectives were chunks are designated for a particular GPU (e.g. AllToAll) 
    # only interleaved replication will be correct
    # Interleaved policy only supports single count sends/receives from the input/output buffer
    # (multicount ops are fine between scratch)
    def replicate(self, instances, interleaved):
        if instances == 1:
            self.instanced_tbs = self.tbs
            return 

        self.instanced_tbs = []
        for _ in range(self.num_ranks):
            self.instanced_tbs.append({})

        def is_scratch(buffer):
            return buffer != Buffer.input and buffer != Buffer.output

        def get_new_index(rank, buffer, index, size, i):
            # Scratch buffers always use batched
            if is_scratch(buffer):
                buf_instance_len = self.buffers[rank][buffer].instance_size()
                return buf_instance_len * i + index
            # If this is operating on the input/output buffer then replication strategy can be either interleaved or batched
            # This is to fit with the semantics of certain collectives
            elif interleaved:
                return  index * instances + i * size
            else:
                return  len(self.buffers[rank][buffer]) * i + index

        def get_instance_ref(ref):
            iindex = get_new_index(ref.rank, ref.buffer, ref.index, ref.size, i)
            iref = ChunkRef(ref.rank, ref.buffer, iindex, ref.size)
            return iref

        for i in range(instances):
            # Generate all the threadblocks and ops
            for rank, rank_tbs in enumerate(self.tbs):
                rank_channels = self.num_channels [rank]
                for tbid, tb in rank_tbs.items():
                    instance_channel = rank_channels * i + tb.channel
                    itb = Threadblock(instance_channel, tb.send, tb.recv)
                    itbid = tbid * instances + i
                    itb.ops = [None] * len(tb.ops)
                    for s, op in enumerate(tb.ops):
                        isrc = get_instance_ref(op.src)
                        idst = get_instance_ref(op.dst)
                        idepends = [] 
                        # Note: We don't need the fill out the rest of the metadata since replication is the last optimization
                        iop = Op(op.inst, op.rank, isrc, idst, idepends, op.step, itbid) 
                        itb.ops[s] = iop
                    self.instanced_tbs[op.rank][itbid] = itb
        
        # Redo dependency analysis
        for rank, rank_tbs in enumerate(self.tbs):
            for tbid, tb in rank_tbs.items():
                for i in range(instances):
                    itbid = tbid * instances + i
                    itb = self.instanced_tbs[rank][itbid]
                    for op, iop in zip(tb.ops, itb.ops):
                        iop.depends = [None] * len(op.depends)
                        for s, dep in enumerate(op.depends):
                            dep_tbid = dep.tb
                            dep_itbid = dep_tbid * instances + i
                            dep_step = dep.step
                            iop.depends[s] = self.instanced_tbs[op.rank][dep_itbid].ops[dep_step] 

