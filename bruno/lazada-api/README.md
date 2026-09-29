# Lazada Integration API — Bruno Collection

วิธีเปิด:

1. เปิดแอป [Bruno](https://www.usebruno.com/)
2. `Open Collection` → เลือกโฟลเดอร์ `bruno/lazada-api` นี้
3. มุมขวาบน เลือก environment **Local** แล้วกรอกค่าใน `portal_token` / `partner_slug` / `line_access_token` / `lazada_order_number` ให้ตรงกับ instance ที่จะทดสอบ (ดูรายละเอียดแต่ละตัวแปรในหน้า docs ของ collection เอง — คลิกชื่อ collection ด้านซ้าย)

โฟลเดอร์:

- **Portal** — พนักงาน partner ตั้งค่า/เชื่อมต่อ Lazada shop (auth: portal admin token)
- **Member** — สมาชิก LINE กรอกเลข order เพื่อขอแต้ม (auth: LINE access token)
- **Integrations** — OAuth callback ที่ Lazada เรียกกลับมาเอง (ไว้อ้างอิง ไม่ได้มีไว้ยิงตรง)

ทุก request มี tab **Docs** อธิบาย request/response/error case ไว้ครบ อ้างอิงจากโค้ดจริงใน
`src/crm_custom/controllers/portal/lazada.py`, `src/crm_custom/controllers/user/lazada_claim.py`,
`src/crm_custom/controllers/integrations/lazada.py`, `src/crm_custom/models/partner/lazada_integration.py`,
`src/crm_custom/models/partner/lazada_order_claim.py`
