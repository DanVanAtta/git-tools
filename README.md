# git_tools

Helper scripts to make working with git easier.

## Installation

- Clone this repository.
- Add the cloned folder to your path
- 'gup-all' needs uv (https://docs.astral.sh/uv/) on your path; it runs itself through 'uv run --script'.


## Various additional useful commands

### Delete Merged Branches
```
git branch --merged | \
   grep -v \* | \
   grep -v "main" | \
   xargs git branch -D
```


