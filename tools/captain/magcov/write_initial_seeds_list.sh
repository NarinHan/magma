#!/usr/bin/env bash

find $TARGET/corpus/$PROGRAM -maxdepth 1 -type f -printf "%f\n" > /magma/magcov/initial_seeds.txt
