# Review Comments

## Refactor

- There is dead code, example Claude driver have the two methods to load agents, only one is used
- Code seams brittle, it depends a lot on certain format in md files.
- run_sensors_with_retry seams to run all sensors and then check? Is that correct? It seams like it should run the
  cheapest first, i.e. lint and fix all those problems. Some if those errors can be fixed without calling an agent. Then
  when lint and LSP checks are done, we do tests. It the tests fails and we edit, then lint/lsp should run again to ok,
  then tests. Just as a person would to or a ci/cd pipeline, run cheep checks often and early and more expensive checks.
- The loop method is very long. It as has the correct flow with different sections, but it is a lot to sctroll thought
  to see the flow. To make the flow more visible we should break out more methods.
- I see that status.md is included in all calls to driver. I see a risk with context growing over time. Any suggestions
  here.
- it seems like we need to get the code health in there so that we don't keep writing code like this. And should we have
  a refactor sub agent that we can run to find refactor opportunities? (similar to the plan agent)
