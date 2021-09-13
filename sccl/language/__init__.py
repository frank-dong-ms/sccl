# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


from dataclasses import dataclass
from enum import Enum
from sccl.language.ir import *
from sccl.language.passes import *
import sccl.collectives as collectives

# Initializes input buffer for an alltoall
def alltoall_init_buffers(prog, instances):
    num_ranks = prog.topo.num_nodes()
    chunks_per_node = num_ranks * instances
    for r in range(num_ranks):
        input_buffer = [None] * chunks_per_node
        output_buffer = [None] * chunks_per_node
        for index in range(chunks_per_node):
            chunk = Chunk(r, index, index//instances, index % instances + r*instances)
            input_buffer[index] = chunk
        buffers = {Buffer.input : input_buffer, 
                   Buffer.output : output_buffer}
        prog.ranks.append(Process(prog, r, buffers))
            
# Expected output buffer for alltoall
def alltoall_expected_output(prog, instances):
    num_ranks = prog.topo.num_nodes()
    correct = True
    for r in range(num_ranks):
        output = prog.ranks[r].buffers[Buffer.output]
        for i in range(num_ranks):
            for ch in range(instances):
                index = ch + i * instances
                chunk = output[index]
                expected_origin_index = ch + r * instances
                if chunk is None or chunk.origin_rank != i or chunk.origin_index != expected_origin_index:
                    print(f'Rank {r} chunk {index} is incorrect should be chunk({i},{expected_origin_index}) given {chunk}')
                    correct = False
    return correct

# Initializes input buffer for an allgather
def allgather_init_buffers(prog):
    num_ranks = prog.topo.num_nodes()
    for r in range(num_ranks):
        # Chunk starts on rank r at index 0, ends up on all ranks (-1) at index r
        input_buffer = [Chunk(r, 0, -1, r)]
        output_buffer = [None] * num_ranks
        buffers = {Buffer.input : input_buffer, 
                   Buffer.output : output_buffer}
        prog.ranks.append(Process(prog, r, buffers))
            
# Expected output buffer for allgather
def allgather_expected_output(prog):
    num_ranks = prog.topo.num_nodes()
    correct = True
    for r in range(num_ranks):
        output = prog.ranks[r].buffers[Buffer.output]
        for i in range(num_ranks):
            chunk = output[i]
            if chunk is None or chunk.origin_rank != i or chunk.origin_index != 0:
                print(f'Rank {r} chunk {i} is incorrect should be ({i}, 0) given {chunk}')
                correct = False
    return correct

def allreduce_init_buffers(prog, instances):
    num_ranks = prog.topo.num_nodes()
    chunks_per_node = instances
    for r in range(num_ranks):
        input_buffer = []
        output_buffer = [None] * chunks_per_node
        for c in range(chunks_per_node):
            # TODO: Chunk starts at rank r index c, and ends on all ranks (-1) at index r, also reduced (?? how to indicate??)
            input_buffer.append(Chunk(r, c, -1, c))
        buffers = {Buffer.input : input_buffer, 
                Buffer.output : output_buffer}
        prog.ranks.append(Process(prog, r, buffers))
            

def allreduce_expected_output(prog, instances):
    num_ranks = prog.topo.num_nodes()
    chunks_per_node = instances
    expected_chunks = []

    for c in range(chunks_per_node):
        chunk = ReduceChunk([])
        for r in range(num_ranks):
            chunk = chunk.reduce(Chunk(r, c))
        expected_chunks.append(chunk)

    correct = True
    for r in range(num_ranks):
        output = prog.ranks[r].buffers[Buffer.input]
        for c in range(chunks_per_node):
            chunk = output[c]
            if chunk is None or chunk != expected_chunks[c]:
                print(f'Rank {r} chunk {c} is incorrect should be {expected_chunks[c]} given {chunk}')
                correct = False
    return correct




_current_program = None
def _curr():
    global _current_program
    if _current_program == None:
        raise RuntimeError("No Program in context")
    return _current_program

class SCCLProgram:
    def __init__(self, name, topo, collective, instances):
        self.name = name
        self.topo = topo
        self.collective = collective       
        self.ranks = []
        self.instances = instances
        self.run_opt = True # Runs optimization passes
        # Initialize the input buffers
        if self.collective == 'alltoall':
            alltoall_init_buffers(self, instances)
        elif self.collective == 'allgather':
            allgather_init_buffers(self)
        elif self.collective == 'allreduce':
            allreduce_init_buffers(self, instances)

    # Returns a Process corresponding to rank number
    def rank(self, rank):
        return self.ranks[rank]

    # Checks that all chunks that should be on each rank
    # are present in the output buffer.
    def check(self):
        if self.collective == 'alltoall':
            return alltoall_expected_output(self, self.instances)
        elif self.collective == 'allgather':
            return allgather_expected_output(self)
        elif self.collective == 'allreduce':
            return allreduce_expected_output(self, self.instances)
        return False

    # Lower program to XML
    def lower(self):
        for rank in self.ranks:
            if self.run_opt:
                rank.optimize()
            rank.lower_buffers()
        gpu_prgms = [rank.lower_tbs() for rank in self.ranks]
        return Program(self.name, gpu_prgms)

    def __enter__(self):
        global _current_program
        if _current_program != None:
            raise RuntimeError("There is already a SCCL Program in context")
        _current_program = self
    
    def __exit__(self, exc_type, exc_value, exc_traceback):
        global _current_program
        if _current_program != self:
            raise RuntimeError("This program is not currently in context")
        _current_program = None

def Rank(index):
    return _curr().rank(index)

def XML():
   print(ir_to_xml(_curr().lower()))

def Check():
    return _curr().check()

# Scratch buffer slice with manual indexing
class BufferSlice:
    def __init__(self, buf):
        self.buf = buf
        self.offset = -1 # Offset into the global scratch buffer
        self.index = 0 # Current index of chunks added
        self.chunks = {}

    # Returns the global index into the scratch buffer
    def get_global_index(self, index):
        assert (self.offset > -1), 'set_offset needs to be called first'
        return self.offset + index

    def get_buffer(self):
        return self.buf

    def size(self):
        return len(self.chunks)

    def set_offset(self, offset):
        self.offset = offset

    def get_next_index(self, size):
        index = self.index
        self.index += size
        return index

    def __getitem__(self, index):
        return self.chunks[index]
    
    def __setitem__(self, index, value):
        self.chunks[index] = value


class Process:
    def __init__(self, prog, rank, buffers):
        self.prog = prog
        self.rank = rank
        self.buffers = buffers
        self.tbs = {}
        self.tb_mapping = {}
        self.tb_count = 0

        # Track all the dependencies for each slot in a buffer. 
        # Initially all input chunks have no dependencies.
        # TODO: Should we track all instructions per slot?
        self.slot_ops = {} # (buffer, idx) -> list of ops on this slot

    def _get_tbid(self, inst, other_rank, ch):
        key = (inst, other_rank, ch)
        if key in self.tb_mapping:
            tbid = self.tb_mapping[key]
        else:
            self.tb_mapping[key] = self.tb_count
            tbid = self.tb_count
            self.tb_count += 1
        return tbid

    def _add_send(self, tbid, step, ch, op):
        assert(op.inst == Instruction.send)
        sendto = op.dst.rank

        # Update tb and assign to a default tb if not given
        if tbid == -1:
            tbid = self._get_tbid(Instruction.send, sendto, ch)

        if tbid not in self.tbs:
            self.tbs[tbid] = Threadblock(ch, send=sendto, ops={step: op})
        else:
            tb = self.tbs[tbid]
            assert (tb.send == -1 or tb.send == sendto), \
                f'Rank {self.rank}: Threadblock {tbid} is already set to send to {tb.send}, trying to send to {sendto}'
            tb.send = sendto
            assert step not in tb.ops, f'Step {step} already in rank {self.rank} tbid {tbid}'
            tb.ops[step] = op

        # Fill in op dependence 
        op.tb = tbid
        op.depends = self._get_dependences(op.src.buffer, op.src.index, op.src.size)
        
        # Update slot_ops 
        for i in range(op.src.size):
            self._update_slot_ops(op.src.buffer, op.src.index, op, tbid)

        
    def _add_recv(self, tbid, step, ch, op):
        assert(op.inst == Instruction.recv)
        receivefrom = op.src.rank
        if tbid == -1:
            tbid = self._get_tbid(Instruction.recv, receivefrom, ch)
        recvd_chunkref = op.dst

        # Fill in op dependence 
        op.tb = tbid
        op.depends = self._get_dependences(op.src.buffer, op.src.index, op.src.size)

        # Update buffer with received chunks, update dependencies for those chunks
        for i in range(op.src.size):
            self.buffers[op.dst.buffer][op.dst.index+i] = self.prog.ranks[op.src.rank].buffers[op.src.buffer][op.src.index+i]
            dependence_key = (op.dst.buffer, op.dst.index+i)
            self._update_slot_ops(op.dst.buffer, op.dst.index+i, op, tbid)

        # Update tbs
        if tbid not in self.tbs:
            self.tbs[tbid] = Threadblock(ch, recv=receivefrom, ops={step: op})
        else:
            tb = self.tbs[tbid]
            assert (tb.recv == -1 or tb.recv == receivefrom), \
                   f'Rank {self.rank}: Threadblock {tbid} is already set to receive from {tb.recv}, trying to receive from {receivefrom}'
            tb.recv = receivefrom
            assert step not in tb.ops, f'Step {step} in rank {self.rank} tbid {tbid}'
            tb.ops[step] = op

    def _add_copy(self, tbid, step, ch, op):
        assert(op.inst == Instruction.copy)
        if tbid == -1:
            tbid = self._get_tbid(Instruction.copy, -1, ch)

        # Fill in op dependence 
        op.tb = tbid
        op.depends = self._get_dependences(op.src.buffer, op.src.index, op.src.size)

        # Update buffer copied chunks and dependencies
        for i in range(op.src.size):
            self.buffers[op.dst.buffer][op.dst.index+i] = self.buffers[op.src.buffer][op.src.index+i]
            self._update_slot_ops(op.dst.buffer, op.dst.index+i, op, tbid)
        
        # Update tbs
        if tbid not in self.tbs:
            self.tbs[tbid] = Threadblock(ch, ops={step: op})
        else:
            tb = self.tbs[tbid]
            tb.ops[step] = op

    def _add_receive_reduce_copy(self, tbid, step, ch, op):
        receivefrom = op.src.rank
        # TODO: rrc and recv share a tb
        if tbid == -1:
            tbid = self._get_tbid(Instruction.recv, receivefrom, ch) 
        recvd_chunkref = op.dst

        # Fill in op dependence 
        op.tb = tbid
        op.depends = self._get_dependences(op.src.buffer, op.src.index, op.src.size)

        # Update buffer with reduced chunks and dependencies for each chunk
        for i in range(op.src.size):
            reduce_chunk = self.buffers[op.dst.buffer][op.dst.index+i]
            other_chunk = self.prog.ranks[op.src.rank].buffers[op.src.buffer][op.src.index+i]
            self.buffers[op.dst.buffer][op.dst.index+i] = reduce_chunk.reduce(other_chunk)
            self._update_slot_ops(op.dst.buffer, op.dst.index+i, op, tbid)

        # Update tbs
        if tbid not in self.tbs:
            self.tbs[tbid] = Threadblock(ch, recv=receivefrom, ops={step: op})
        else:
            tb = self.tbs[tbid]
            assert (tb.recv == -1 or tb.recv == receivefrom), \
                   f'Rank {self.rank}: Threadblock {tbid} is already set to receive from {tb.recv}, trying to receive from {receivefrom}'
            tb.recv = receivefrom
            assert step not in tb.ops, f'Step {step} in rank {self.rank} tbid {tbid}'
            tb.ops[step] = op

    def _add_reduce(self, tbid, step, ch, op):
        receivefrom = op.src.rank
        if tbid == -1:
            tbid = self._get_tbid(Instruction.copy, receivefrom, ch) # TODO: copy and reduce share tb
        recvd_chunkref = op.dst

        # Fill in op dependence 
        op.tb = tbid
        op.depends = self._get_dependences(op.src.buffer, op.src.index, op.src.size)

        # Update buffer with reduced chunk and dependences
        for i in range(op.src.size):
            reduce_chunk = self.buffers[op.dst.buffer][op.dst.index+i]
            other_chunk = self.prog.ranks[op.src.rank].buffers[op.src.buffer][op.src.index+i]
            self.buffers[op.dst.buffer][op.dst.index+i] = other_chunk.reduce(reduce_chunk)
            self._update_slot_ops(op.dst.buffer, op.dst.index+i, op, tbid)

        # Update tbs
        if tbid not in self.tbs:
            self.tbs[tbid] = Threadblock(ch, recv=receivefrom, ops={step: op})
        else:
            tb = self.tbs[tbid]
            assert (tb.recv == -1 or tb.recv == receivefrom), \
                   f'Rank {self.rank}: Threadblock {tbid} is already set to receive from {tb.recv}, trying to receive from {receivefrom}'
            tb.recv = receivefrom
            assert step not in tb.ops, f'Step {step} in rank {self.rank} tbid {tbid}'
            tb.ops[step] = op
                
    # TODO: This might be broken
    def _get_dependences(self, buffer, start_index, size):
        # Get and merge dependencies for each index
        # If multiple dependencies for same tb keep the one with the highest step
        depends = {}
        for i in range(size):
            index = start_index + i
            key = (buffer, index)
            if key in self.slot_ops:
                last_op = self.slot_ops[key][-1]
                tb = last_op.tb
                if tb not in depends or last_op.step > depends[tb].step:
                    depends[tb] = last_op
        return list(depends.values())
    
    def _update_slot_ops(self, buffer, index, op, tbid):
        key = (buffer, index)
        if key in self.slot_ops:
            self.slot_ops[key].append(op)
        else:
            self.slot_ops[key] = [op]

    def get_ref(self, buffer, index, size):
        return Ref(buffer, index, size, self.prog, self.rank)

    # Returns a reference to the chunk located at index of the input buffer.
    def input(self, index, size=1):
        return self.get_ref(Buffer.input, index, size)

    # Creates a scratch buffer with a name
    def create_scratch(self, name):
        assert (name not in self.buffers), f'Scratch buffer, {name}, already created'
        self.buffers[name] = BufferSlice(Buffer.scratch)

    # Runs optimization pass over slot_ops
    def optimize(self):
        for k, ops in self.slot_ops.items():
            rrcs_rrs(ops, self.tbs)
            rcs(ops, self.tbs)

    # Convert local scratch buffers to index into one global scratch buffer
    def lower_chunk(self, chunk):
        if chunk.buffer is not Buffer.input and chunk.buffer is not Buffer.output:
            rank = self.prog.ranks[chunk.rank]
            buffer = rank.buffers[chunk.buffer].get_buffer()
            index = rank.buffers[chunk.buffer].get_global_index(chunk.index)
            return ChunkRef(buffer, index, chunk.size)
        return chunk

    # Assigns each scratch buffer an offset into the global scratch buffer
    def lower_buffers(self):
        offset = 0
        for key, buf in self.buffers.items():
            if key is not Buffer.input and key is not Buffer.output:
                buf.set_offset(offset)
                offset += buf.size()

    # Preprocess the threadblocks for lowering into xml
    def lower_tbs(self):
        for tb in self.tbs.values():
            # Sort Ops by step
            # Index scratch buffers
            tb_ops = []
            for _, op in sorted(tb.ops.items()):
                op.src = self.lower_chunk(op.src)
                op.dst = self.lower_chunk(op.dst)
                tb_ops.append(op)
            tb.ops = tb_ops
        return Gpu(self.rank, self.tbs.values())

@dataclass
class Chunk:
    origin_rank: int # Rank the chunk initially started at
    origin_index: int # Index the chunk initially started at
    dst_rank: int = -1
    dst_index: int = -1

    def reduce(self, chunk):
        if type(chunk) is ReduceChunk:
            return chunk.reduce(self)
        elif type(chunk) is Chunk:  
            chunks = [self, chunk]
            return ReduceChunk(chunks)
        else:
            assert True, "Trying to reduce with chunk of None"
            return None

    def __eq__(self, other):
        return type(other) is Chunk and self.origin_rank == other.origin_rank and self.origin_index == other.origin_index

    def __lt__(self, other):
        return self.origin_rank < other.origin_rank or \
               (self.origin_rank == other.origin_rank and self.origin_index < other.origin_index)

@dataclass
class ReduceChunk:
    chunks: list # List of chunks reduced

    def reduce(self, chunk):
        if type(chunk) is ReduceChunk:
            chunks = self.chunks + chunk.chunks
        elif type(chunk) is Chunk:  
            chunks =self.chunks + [chunk]
        else:
            assert True, "Trying to reduce with chunk of None"
        return ReduceChunk(chunks)

    def sort(self):
        self.chunks.sort()

    # Two reduce chunks are equal if they contain the same list of
    # chunks being reduced
    # Assume commutativity so order does not matter (TODO: check)
    def __eq__(self, other):
        self.sort()
        other.sort()
        return self.chunks == other.chunks


@dataclass
class Ref(ChunkRef):
    prog: SCCLProgram
    rank: int
    missing: set = field(default_factory=set)

    def __repr__(self):
        return f'Ref(Buffer:{self.buffer}, Index:{self.index}, Size:{self.size}, Rank:{self.rank})'

    def _end(self):
        return self.index + self.size

    def _get_chunk(self, index):
        return self.prog.ranks[self.rank].buffers[self.buffer][index]

    def _copy(self, buffer=Buffer.output, index=-1, step=-1, tb=-1, ch=0):
        dst_chunkref = self.prog.ranks[self.rank].get_ref(buffer, index, self.size)
        op = Op(Instruction.copy, self, dst_chunkref, {}, step)
        self.prog.ranks[self.rank]._add_copy(tb, step, ch, op)
        return dst_chunkref

    def split(self, num):
        assert (self.size % num == 0), f'Trying to split a chunk of {self.size} elements into {num} parts'
        chunks = [None] * num
        size = self.size // num
        for i in range(num):
            index = self.index + i * size
            chunks[i] = Ref(self.buffer, index, size, self.prog, self.rank, {}) # TODO: Broken
        return chunks

    # TODO: this is weird...
    def group(self, other):
        assert (self.rank == other.rank), f'Trying to concatenate chunks on ranks {self.rank} and {other.rank}'
        assert (self.buffer == other.buffer), f'Trying to concatenate chunks in {self.buffer} and {other.buffer}'
        if self.index < other.index:
            first = self
            second = other
        else:
            first = other
            second = self

        end = max(first._end(), second._end())
        missing = set(range(first.index, end))
        missing.difference_update(set(range(first.index, first._end())).difference(first.missing))
        missing.difference_update(set(range(second.index, second._end())).difference(second.missing))
        # print(first.index, first.size, second.index, second.size, missing)
        return Ref(self.buffer, first.index, end - first.index, self.prog, self.rank, missing) # Broken
        

    def send(self, dst, buffer=None, index=-1, step=-1, sendtb=-1, recvtb=-1, ch=0):
        assert (len(self.missing) == 0), f'Trying to send an incomplete concatenation. Missing indices {self.missing}'
        # TEMP TODO FIX: can't run optimization passes with multi chunk sends currently
        if self.size > 1:
            self.prog.run_opt = False

        # If index is not specified assume it is going to the same place in the next gpu
        if index == -1 and buffer == None:
            index = self.index
            buffer = self.buffer # TODO: eventually change this dst buffer depending if it is inplace/outofplace
        elif index == -1 and buffer is not Buffer.input and buffer is not Buffer.output:
            index = self.prog.ranks[dst].buffers[buffer].get_next_index(self.size)

        # Local copy
        if dst == self.rank:
            return self._copy(buffer, index, step, sendtb, ch)

        # Direct send
        assert (self.prog.topo.link(self.rank, dst)), f'No link from {self.rank} to {dst}'
        dst_chunkref = self.prog.ranks[dst].get_ref(buffer, index, self.size)
        sendOp =  Op(Instruction.send, self, dst_chunkref, {}, step)
        self.prog.ranks[self.rank]._add_send(sendtb, step, ch, sendOp)
        receiveOp = Op(Instruction.recv, self, dst_chunkref, {}, step)
        self.prog.ranks[dst]._add_recv(recvtb, step, ch, receiveOp)
        return dst_chunkref

    def _local_reduce(self, buffer, index, step, tb, ch):
        # TODO: Test this out
        dst_chunkref = self.prog.ranks[dst].get_ref(buffer, index, self.size)
        op = Op(Instruction.reduce, self, dst_chunkref, {}, step)
        self.prog.ranks[self.rank]._add_reduce(tb, step, ch, op)
        return dst_chunkref
    
    def reduce(self, dst, buffer, index=-1, step=-1, sendtb=-1, recvtb=-1, ch=0):
        # TODO: wip - would want to decide wether to use a rrc, rrs, rrcs based on what is happening next...
        # (rrc -> send) => replace with rrs if chunk is not fully reduced, else replace with rrcs
        # Local reduce
        if dst == self.rank:
            return self._local_reduce(buffer, index, step, sendtb, ch)
        # Receive reduce copy   
        dst_chunkref = self.prog.ranks[dst].get_ref(buffer, index, self.size)
        sendOp = Op(Instruction.send, self, dst_chunkref, {}, step)
        self.prog.ranks[self.rank]._add_send(sendtb, step, ch, sendOp)
        rrcOp = Op(Instruction.recv_reduce_copy, self, dst_chunkref, {}, step)
        self.prog.ranks[dst]._add_receive_reduce_copy(recvtb, step, ch, rrcOp)
        return dst_chunkref

    def get_origin_index(self, index=0):
        return self._get_chunk(index + self.index).origin_index

    def get_origin_rank(self, index=0):
        return self._get_chunk(index + self.index).origin_rank

    def get_dst_index(self, index=0):
        return self._get_chunk(index + self.index).dst_index

    def get_dst_rank(self, index=0):
        return self._get_chunk(index + self.index).dst_rank

    def print_chunk_info(self, index=0):
        print(self._get_chunk(index + self.index)) 