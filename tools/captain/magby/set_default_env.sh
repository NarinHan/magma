#!/bin/bash

export FUZZER="libfuzzer"
export TARGET="libpng"
export PROGRAM="libpng_read_fuzzer"
export SHARED="workdir"
export POLL="5"
export TIMEOUT="1h"
