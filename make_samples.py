"""生成多格式测试样本（md/txt/docx），用于验证文件夹批量摄取。"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
os.makedirs(SAMPLES, exist_ok=True)

# --- docx（标准库构造最小合法 docx）---
document_xml = (
    '<?xml version="1.0"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body>'
    '<w:p><w:r><w:t>企业版 SLA 服务等级协议</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>核心服务可用性承诺为 99.95%，月度故障时长不超过 21.9 分钟。</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>工单响应：P0 紧急 15 分钟内响应，P1 高危 1 小时，P2 普通 4 小时。</w:t></w:r></w:p>'
    '</w:body></w:document>'
)
content_types = (
    '<?xml version="1.0"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)
rels = (
    '<?xml version="1.0"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '</Relationships>'
)
docx_path = os.path.join(SAMPLES, "sla手册.docx")
with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("[Content_Types].xml", content_types)
    z.writestr("_rels/.rels", rels)
    z.writestr("word/document.xml", document_xml)

# --- md ---
md_path = os.path.join(SAMPLES, "sla说明.md")
with open(md_path, "w", encoding="utf-8") as f:
    f.write(
        "# SLA 说明\n"
        "本手册说明企业客户服务等级。\n"
        "- 可用性：99.95%\n"
        "- 响应：P0 15 分钟，P1 1 小时，P2 4 小时\n"
    )

# --- txt ---
txt_path = os.path.join(SAMPLES, "常见问题.txt")
with open(txt_path, "w", encoding="utf-8") as f:
    f.write(
        "如何重置密码？在登录页点击忘记密码，通过绑定手机或邮箱验证码重置。"
        "企业子账号重置需管理员审批。\n"
    )

print("samples ready:", os.listdir(SAMPLES))
