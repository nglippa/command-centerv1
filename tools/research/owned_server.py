"""Wait for durable ownership registration before execing the local server.

EOF means the worker disappeared before permitting execution: exit without loading.
The pipe permission is sent only after PID/birth/target identity have been committed.
"""
import json
import os
import sys


def main():
    command=json.loads(sys.argv[1])
    if sys.stdin.buffer.readline()!=b"START\n": return 0
    null=os.open(os.devnull,os.O_RDONLY)
    os.dup2(null,0); os.close(null)
    os.execvpe(command[0],command,os.environ)


if __name__=="__main__": raise SystemExit(main())
