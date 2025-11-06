## magby
- Magby (magma + baby) is to help running the fuzzing experiments conveniently!
- It creates the workspace directory under `mamga/tools/captain/experiments` where magma sends logs and data while fuzzing.
   - It keeps the name of the workspace to be unique by tagging with date and numbering.
- It sets the default values for mamga configuration with `set_default_env.sh`. 
- It provides various options to support automated fuzzing experiments.
   - `--set`: overrides the default configuration values
   - `--cmd`: runs the command after it creates the workspace
   - `--tmux`: runs the experiment in tmux session
   - check out more with `--help`

### **How to run?**
- Run in `magma/tools/captain`
- **`python3 magby/mkexp.py --cmd ./start.sh --tmux [name-of-the-workspace]`**
   - This will run `magma/tools/captain/start.sh` in tmux session.

⬇️  magby from Pokemon 

![magby](https://pokestop.io/img/pokemon/magby-256x256.png)
