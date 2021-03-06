import os
import time
import hashlib
import logging
import subprocess
import traceback

import redis
from celery import Celery
import torndb

from web.settings import *
import config
from fuzzer import Fuzzer, InstallError
from concolic import Concolic, pcap

l = logging.getLogger("mining.tasks")

db = torndb.Connection(db_server, db_database, db_username, db_password)

redis_url = "redis://%s:%d" % (config.REDIS_HOST, config.REDIS_PORT)
app = Celery('tasks', broker=redis_url, backend=redis_url)
app.conf.CELERY_ROUTES = config.CELERY_ROUTES
app.conf['CELERY_ACKS_LATE'] = True
app.conf['CELERYD_PREFETCH_MULTIPLIER'] = 1

redis_pool = redis.ConnectionPool(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB)

def get_fuzzer_id(input_data_path):
    # get the fuzzer id
    abs_path = os.path.abspath(input_data_path)
    if "sync/" not in abs_path or "id:" not in abs_path:
        l.warning("path %s, cant find fuzzer id", abs_path)
        return "None"
    fuzzer_name = abs_path.split("sync/")[-1].split("/")[0]
    input_id = abs_path.split("id:")[-1].split(",")[0]
    return fuzzer_name + ",src:" + input_id

@app.task
def drill(binary, input_data, bitmap_hash, tag):
    redis_inst = redis.Redis(connection_pool=redis_pool)
    fuzz_bitmap = redis_inst.hget(binary + '-bitmaps', bitmap_hash)

    binary_path = os.path.join(config.BINARY_DIR, binary)
    concolic = Concolic(binary_path, input_data, fuzz_bitmap, tag, redis=redis_inst)
    try:
        return concolic.drill()
    except Exception as e:
        traceback.print_exc()
        l.error("encountered %r exception when drilling into \"%s\"", e, binary)
        l.error("input was %r", input_data)

def input_filter(fuzzer_dir, inputs):

    traced_cache = os.path.join(fuzzer_dir, "traced")

    traced_inputs = set()
    if os.path.isfile(traced_cache):
        with open(traced_cache, 'rb') as f:
            traced_inputs = set(f.read().split('\n'))

    new_inputs = filter(lambda i: i not in traced_inputs, inputs)

    with open(traced_cache, 'ab') as f:
        for new_input in new_inputs:
            f.write("%s\n" % new_input)

    return new_inputs

def request_drilling(fzr):
    '''
    request a drilling job on a fuzzer object

    :param fzr: fuzzer object to request drilling on behalf of, this is needed to fine the input input queue
    :return: list of celery AsyncResults, we accumulate these so we can revoke them if need be
    '''

    d_jobs = [ ]

    bitmap_f = os.path.join(fzr.out_dir, "fuzzer-1", "fuzz_bitmap")
    bitmap_data = open(bitmap_f, "rb").read()
    bitmap_hash = hashlib.sha256(bitmap_data).hexdigest()

    redis_inst = redis.Redis(connection_pool=redis_pool)
    redis_inst.hset(fzr.binary_id + '-bitmaps', bitmap_hash, bitmap_data)

    in_dir = os.path.join(fzr.out_dir, "fuzzer-1", "queue")

    # ignore hidden files
    inputs = filter(lambda d: not d.startswith('.'), os.listdir(in_dir))

    # filter inputs which have already been sent to concolic
    inputs = input_filter(os.path.join(fzr.out_dir, "fuzzer-1"), inputs)

    # submit a concolic job for each item in the queue
    for input_file in inputs:
        input_data_path = os.path.join(in_dir, input_file)
        input_data = open(input_data_path, "rb").read()
        l.info("[%s] concolic being requested! %s" % (fzr.binary_path, input_data_path))
        d_jobs.append(drill.delay(fzr.binary_id, input_data, bitmap_hash, get_fuzzer_id(input_data_path)))

    return d_jobs

def start_listener(fzr):
    '''
    start a listener for concolic inputs
    '''

    concolic_queue_dir = os.path.join(fzr.out_dir, "concolic", "queue")
    channel = "%s-generated" % fzr.binary_id

    # find the bin directory listen.py will be installed in
    base = os.path.dirname(__file__)

    # while not "bin" in os.listdir(base) and os.path.abspath(base) != "/":
    #     base = os.path.join(base, "..")

    if os.path.abspath(base) == "/":
        raise Exception("could not find concolic listener install directory")

    args = [os.path.join(base, "listen.py"), concolic_queue_dir, channel]
    p = subprocess.Popen(args)

    # add the proc to the fuzzer's list of processes
    fzr.procs.append(p)

def clean_redis(fzr):
    redis_inst = redis.Redis(connection_pool=redis_pool)

    # delete all catalogued inputs
    redis_inst.delete("%s-catalogue" % fzr.binary_id)

    # delete all the traced entries
    redis_inst.delete("%s-traced" % fzr.binary_id)

    # delete the finished entry
    redis_inst.delete("%s-finsihed" % fzr.binary_id)

    # delete the fuzz bitmaps
    redis_inst.delete("%s-bitmaps" % fzr.binary_id)

@app.task
def fuzz(binary):

    l.info("beginning to fuzz \"%s\"", binary)

    binary_path = os.path.join(config.BINARY_DIR, binary)

    seeds = ["1111"]
    # look for a pcap
    pcap_path = os.path.join(config.PCAP_DIR, "%s.pcap" % binary)
    if os.path.isfile(pcap_path):
        l.info("found pcap for binary %s", binary)
        seeds = pcap.process(pcap_path)
    else:
        l.warning("unable to find pcap file, will seed fuzzer with the default")

    # TODO enable dictionary creation, this may require fixing parts of the fuzzer module
    fzr = Fuzzer(binary_path, config.FUZZER_WORK_DIR, config.FUZZER_INSTANCES, time_limit=config.FUZZ_TIMEOUT,
                 qemu=False, seeds=seeds, create_dictionary=False)

    try:
        fzr.start()

        # start a listening for inputs produced by concolic
        start_listener(fzr)

        # clean all stale redis data
        clean_redis(fzr)

        # list of 'concolic request' each is a celery async result object
        concolic_jobs = [ ]

        time.sleep(2);
        # start the fuzzer and poll for a crash, timeout, or concolic assistance
        while not fzr.found_crash() and not fzr.timed_out():
            # check to see if concolic should be invoked
            if 'fuzzer-1' in fzr.stats and 'pending_favs' in fzr.stats['fuzzer-1']:
                if not int(fzr.stats['fuzzer-1']['pending_favs']) > 0:
                    concolic_jobs.extend(request_drilling(fzr))

            time.sleep(config.CRASH_CHECK_INTERVAL)

        # make sure to kill the fuzzers when we're done
        fzr.kill()

    except InstallError:
        l.info("fuzzer InstallError")
        return False

    # we found a crash!
    if fzr.found_crash():
        l.info("found crash for \"%s\"", binary)

        sql = 'update binarys SET status=3 WHERE binary_name = "%s"' % binary
        print (sql)
        db.execute(sql)

        # publish the crash
        redis_inst = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB)
        redis_inst.publish("crashes", binary)

        # revoke any concolic jobs which are still working
        for job in concolic_jobs:
            if job.status == 'PENDING':
                job.revoke(terminate=True)

    if fzr.timed_out():
        l.info("timed out while fuzzing \"%s\"", binary)

        sql = 'update binarys SET status=-1 WHERE binary_name = "%s"' % binary
        db.execute(sql)


    # TODO end drilling jobs working on the binary
    return len(fzr.crashes()) > 0
