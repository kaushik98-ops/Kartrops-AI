#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stack import KartOpsStack

app = cdk.App()
KartOpsStack(app, "KartOpsStack",
             env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                                 region=os.environ.get("CDK_DEFAULT_REGION")))
app.synth()
