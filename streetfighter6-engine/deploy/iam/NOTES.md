# sf6-deployer IAM — Bedrock Titan V2 許可メモ (ADR-017 / M4 ステップ2)

## 症状
`bedrock:InvokeModel` (Titan Embed V2) が `AccessDeniedException`
"explicit deny in an identity-based policy: SF6FrameScraperDeployerPolicy"。

## 根本原因
`SF6FrameScraperDeployerPolicy` の `BedrockDenyAllOtherModels` 文が、
`NotResource` に列挙した Gemma 系**以外**の InvokeModel を明示 Deny している。
Titan の ARN がリストに無いため Deny 対象となり、末尾の `InvokeTitanEmbedV2`
Allow は明示 Deny に勝てず無効。

## 修正 (1行追加)
`BedrockDenyAllOtherModels` の `NotResource` 配列に以下を追加してDenyの例外にする:

    "arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.titan-embed-text-v2:0"

`InvokeTitanEmbedV2` Allow 文 (bedrock-invoke-titan.json) は残す。

## 検証
    ./.venv312/bin/python -c "import boto3,json; \
      c=boto3.client('bedrock-runtime',region_name='ap-northeast-1'); \
      print(len(json.loads(c.invoke_model(modelId='amazon.titan-embed-text-v2:0', \
      body=json.dumps({'inputText':'x','dimensions':1024,'normalize':True}))['body'].read())['embedding']))"
    # → 1024 が出れば OK

## 注意
本番 MCP (Lambda) は sf6-deployer ではなく専用実行ロールで Bedrock を使う。
deployer への Titan 許可はローカル再埋め込み用。
