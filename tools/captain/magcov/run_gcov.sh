#!/bin/bash

find /magma/targets/libpng/repo -name "*.gcda" | while read -r file; do
    dir=$(dirname "$file")
    base=$(basename "$file" .gcda)

    echo "Running gcov in $dir for $base"
    gcov -o "$dir" "$dir/$base"*
done | tee gcov_results.txt

