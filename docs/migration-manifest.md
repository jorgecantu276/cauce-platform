# Clean-repository migration manifest

Source snapshot: `jorgecantu276/servicios-financieros-beta` at commit `238a3ec2d3cef030773a36e7bfe34e4093879d23`.

Included:

- the active React business workspace, split from the former combined staff application;
- the active React implementation console, split at the platform trust boundary;
- the active Python/DynamoDB payments API and workers;
- the server-rendered public payment page;
- unit and Moto integration tests;
- AWS SAM infrastructure and manual GitHub deployment workflow;
- current architecture and delivery notes.

Explicitly excluded:

- `backend/legacy` and PostgreSQL migrations, because DynamoDB is the active persistence contract;
- recovered SUNWAVE or other unrelated applications;
- archived worktrees, investigation output, build output, dependency directories, and local caches;
- superseded implementation plans and historical conversation artifacts.

The original repository is not deleted or rewritten. It remains the auditable migration source until this repository is accepted.

