# รายงานวิเคราะห์และแก้ไขพอร์ต VLESS / VMess ไม่ตรงกับ 3x-ui

## ขอบเขตที่ตรวจสอบ

ตรวจสอบเส้นทางตั้งแต่การกำหนด Package Template บนเว็บไซต์ การบันทึก/อ่านค่าจาก `network_packages` ไปจนถึงการสร้าง Client, อ่าน Inbound, ดึงลิงก์จาก 3x-ui, แก้ Template, บันทึกลิงก์ และส่งลิงก์ผ่าน Telegram

ไฟล์สำคัญที่ตรวจสอบ:

- `web_admin.py` — ฟอร์มและ API สำหรับตั้งค่า Template ของแพ็กเกจ
- `database.py` — คอลัมน์และการอ่าน/เขียนค่าของ Template
- `main.py` — เส้นทางซื้อแพ็กเกจและทดลองใช้ฟรี
- `xui_api.py` — อ่าน Inbound, สร้างลิงก์ และแก้ URI

## ผลการวิเคราะห์

หน้าเว็บไซต์เก็บ Template แยกเป็น `template_enabled`, `template_address`, `template_network`, `template_host` และ `template_path` โดยไม่มีช่องสำหรับกำหนดพอร์ต การตั้งค่ากลุ่มนี้ไม่ใช่จุดที่ควรใช้แทนพอร์ต Inbound

ในโค้ดเดิม `generate_link()` อ่าน `inbound.port` และ normalize พอร์ตของลิงก์จาก Panel API ก่อน จากนั้น `main.py` จึงเรียก `apply_package_template()` แยกอีกครั้ง ฟังก์ชัน Template ออกแบบมาให้รักษาพอร์ตที่ติดมากับลิงก์เดิม แต่ไม่ได้รับค่า `inbound.port` เพื่อยืนยัน/บังคับพอร์ตเป็นขั้นตอนสุดท้าย

ดังนั้นยังไม่มีการรับประกันที่จุดสุดท้ายก่อนบันทึกและส่งข้อความว่า พอร์ต VLESS endpoint หรือ VMess JSON field `port` ตรงกับ Inbound ที่เลือกอยู่เสมอ

## สิ่งที่แก้ไข

1. เพิ่มพารามิเตอร์ `package` ใน `XUIApi.generate_link()` เพื่อให้สร้างลิงก์และใช้ Template ในเส้นทางเดียวกัน
2. ตรวจสอบ `inbound.port` เป็นจำนวนเต็มช่วง 1–65535 และใช้ค่าที่ตรวจสอบแล้วกับ fallback builder
3. หลัง Template ปรับ Address / Network / Host / Path เสร็จ เรียก `_set_link_port()` อีกครั้งโดยใช้ `inbound.port` เดิมที่อ่านจาก Inbound รอบนั้น
4. สำหรับ VLESS จะบังคับพอร์ตที่ endpoint ของ URI; สำหรับ VMess จะบังคับ JSON field `port` ภายใน Base64 โดยรักษา `id` ไว้
5. เปลี่ยน flow การซื้อแพ็กเกจและทดลองใช้ฟรีใน `main.py` ให้เรียก `generate_link(..., package=...)` รูปแบบเดียวกัน
6. หากบังคับพอร์ตในลิงก์สุดท้ายไม่ได้ จะยกเลิกการส่งลิงก์; flow ซื้อแพ็กเกจจะลบ Client และคืนเครดิต ส่วนทดลองใช้ฟรีจะลบ Client
7. อัปเดตเลขรุ่นเป็น `v56.4` และเพิ่ม Regression tests ใน `tests/test_package_template_port.py`

## ผลการทดสอบ

รันคำสั่ง:

```bash
python -m py_compile *.py tests/*.py
python -m unittest discover -s tests -v
```

ผล: Syntax ผ่าน และ Unit tests ผ่านทั้งหมด 5 รายการ ได้แก่

- VLESS ใช้ Address / Network / Host / Path จาก Template และได้พอร์ตจาก Inbound
- VMess ใช้ Template และได้พอร์ตจาก Inbound โดย `id` ไม่เปลี่ยน
- Template ปิดอยู่ก็ยังบังคับพอร์ตจาก Inbound
- Integration path จำลอง `generate_link()` สำหรับ VLESS
- Integration path จำลอง `generate_link()` สำหรับ VMess

## ข้อจำกัดการยืนยันผล

การทดสอบครั้งนี้ใช้ Unit tests และจำลองคำตอบจาก 3x-ui API ไม่ได้เชื่อมต่อกับ 3x-ui Server จริงของผู้ใช้ เนื่องจากไฟล์ที่แนบไม่มี URL, token หรือข้อมูลเชื่อมต่อ Panel จึงยังต้องยืนยันหลังติดตั้งกับ Inbound จริงอีกครั้ง


---

# รายงานการแก้ไขเพิ่มเติม v56.5 — เพิ่ม Template Port ในแพ็กเกจ

## ข้อกำหนดที่เพิ่ม

เพิ่มช่อง **Port (พอร์ตปลายทาง)** ในส่วน Package Template ของหน้าเว็บไซต์ โดยตั้งค่าแยกตามแพ็กเกจได้ ค่าอนุญาตเป็นจำนวนเต็ม 1–65535 และเว้นว่างเพื่อใช้พอร์ตของ Inbound ที่เลือกใน 3x-ui

## จุดที่แก้ไข

- `web_admin.py`: เพิ่ม `template_port` ใน `PackageBody`, ตรวจสอบช่วงค่า, แสดงช่องกรอกใน `packageModal()`, ส่งค่าผ่าน API และแสดงพอร์ตในตารางแพ็กเกจ
- `database.py`: เพิ่มคอลัมน์ `network_packages.template_port` ชนิด INTEGER ค่าเริ่มต้น 0; ปรับ create/update, runtime migration, package merge, backup compatibility และ metadata ของ Google Sheets
- `xui_api.py`: ให้ Inbound port เป็นค่าเริ่มต้น; เมื่อเปิด Template และตั้ง `template_port` จะเลือกค่านั้นแทน แล้วบังคับลงในลิงก์หลังปรับ Template เสร็จ
- `tests/test_package_template_port.py`: เพิ่ม Regression tests สำหรับ VLESS, VMess, พอร์ตที่กำหนดเอง, Inbound fallback, Template ปิด และพอร์ตผิดช่วง
- `README.md`, `CHANGELOG.txt`, `VERSION.txt`: อัปเดตวิธีใช้และรุ่นโปรแกรมเป็น v56.5

## กติกาการทำงาน

1. เปิด Template + กำหนด Port: ใช้พอร์ต Template ในลิงก์สุดท้าย
2. เปิด Template + เว้น Port: ใช้ `inbound.port` ของ Inbound ที่แพ็กเกจเลือก
3. ปิด Template: ใช้ `inbound.port` เช่นเดิม แม้มีค่า `template_port` ค้างในฐานข้อมูล
4. Address รับเฉพาะโดเมนหรือ IP; ห้ามฝัง `:port` ใน Address และให้กำหนดผ่านช่อง Port เท่านั้น
5. VLESS/Trojan เขียนลงพอร์ต endpoint ของ URI; VMess เขียนลง JSON field `port` ภายใน Base64 โดยไม่เปลี่ยน UUID/รหัสผ่าน
6. หากพอร์ต Template ผิดรูปแบบหรืออยู่นอกช่วง จะไม่ส่งลิงก์ที่ตรวจสอบไม่ได้

## ผลการตรวจสอบ

- `python -m py_compile *.py tests/*.py` — ผ่าน
- `python -m unittest discover -s tests -v` — ผ่าน 12 tests
- ตรวจสอบ Pydantic `PackageBody` และ `_normalize_package_values()` — รับค่าถูกต้อง, ปฏิเสธ 0, ค่าติดลบ, 65536 และทศนิยม
- ทดสอบ SQLite create/update/get — บันทึกและอ่าน Template Port ได้
- ทดสอบฐานข้อมูลรุ่นเก่าที่ไม่มี `template_port`: runtime migration เพิ่มคอลัมน์และตั้งค่าเริ่มต้น 0; package merge และ backup compatibility ทำงานโดยยังรักษาค่า Template เดิม

## ข้อจำกัด

การทดสอบ 3x-ui ใช้ API response จำลอง ยังไม่ได้เชื่อมต่อกับ Panel จริงของผู้ใช้ จึงควรตรวจลิงก์ที่สร้างจาก Inbound จริงหลัง deploy โดยเฉพาะเมื่อมี NAT, reverse proxy หรือ port forwarding ระหว่าง Inbound กับ endpoint ภายนอก
