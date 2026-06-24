Arabic Software Localization Rules

Context of translation : a front end messages and UI component labels and titles for a Data and Analytics / Database software , Do not translate technical terms ,Acronyms ,URLS, terms that has no commonly used translations in the target language


Core Principle

Translate meaning, function, and user intent—not English wording.

Arabic UI text must read as if it was originally written in Modern Standard Arabic (MSA), not translated from English. Prioritize clarity, brevity, and natural Arabic syntax over structural fidelity to the source language.

1. Avoid Literal Translation

Do not mirror English sentence structure.

Incorrect:

* حفظ التغييرات الخاصة بك
* قم بإدخال عنوان البريد الإلكتروني الخاص بك

Preferred:

* حفظ التغييرات
* البريد الإلكتروني

Arabic interfaces should be concise and information-focused.

2. Avoid "تم + مصدر" Constructions

Do not use تم followed by a verbal noun as a default translation of English passive voice.

Incorrect:

* تم تحديث الملف
* تم حذف الرسالة
* تم إنشاء الحساب

Preferred:

* حُدِّث الملف
* حُذفت الرسالة
* أُنشئ الحساب

Or when more natural:

* اكتمل التحديث
* اكتمل الحذف
* أُنشئ الحساب

Use true Arabic passive forms or concise result-oriented expressions.

3. Avoid "القيام بـ"

Do not use القيام بـ to translate simple actions.

Incorrect:

* يرجى القيام بتعديل الحساب
* قم بالضغط على الزر
* القيام بحفظ الملف

Preferred:

* يرجى تعديل الحساب
* اضغط الزر
* حفظ الملف

Use direct verbs whenever possible.

4. Avoid Literal Translation of System Pronouns

Software should not unnecessarily refer to itself as "I" or "we".

Incorrect:

* نحن نعالج طلبك
* نحن نبحث عن التحديثات
* لقد قمنا بحفظ الملف

Preferred:

* جارٍ معالجة الطلب
* جارٍ البحث عن التحديثات
* حُفظ الملف

Favor neutral system language.

5. Do Not Translate "By" as "بواسطة" or "من قبل"

Avoid بواسطة, من قبل, and similar constructions when translating English passive sentences that identify an agent.

Incorrect:

* تم حذف التعليق بواسطة المشرف
* تم حذف التعليق من قبل المشرف
* أُنشئ الملف بواسطة أحمد

Preferred:

* حذف المشرف التعليق
* أنشأ أحمد الملف

When English uses:

* Deleted by Admin
* Created by Ahmed
* Approved by Manager

Translate using a direct active structure:

* حذف المشرف التعليق
* أنشأ أحمد الملف
* اعتمد المدير الطلب

If the UI only displays attribution metadata, use a concise label:

* أنشأه أحمد
* عدّله أحمد
* اعتمده المدير

Never force a passive structure with بواسطة or من قبل when a natural active sentence is possible.

6. Prefer Labels Over Instructions

Field labels should usually be nouns, not commands.

Incorrect:

* أدخل كلمة المرور
* أدخل اسم المستخدم
* اكتب تعليقًا

Preferred:

* كلمة المرور
* اسم المستخدم
* التعليق

Placeholders may contain short examples if necessary.

7. Use Proper Dual Forms

Arabic dual forms must be respected.

Incorrect:

* 2 رسائل
* 2 ملفات
* 2 مستخدمين

Preferred:

* رسالتان
* ملفان
* مستخدمان

When numerals must appear:

* رسالتان (2)
* ملفان (2)

For localization systems supporting plural rules, always implement dedicated dual handling.

8. Apply Correct Arabic Plural Categories

Arabic requires dedicated handling for:

* zero
* one
* two
* few
* many
* other

Never reuse English singular/plural logic.

9. Prefer Nominal UI Labels

Buttons, tabs, and menu items should generally be noun-based.

Preferred:

* حفظ
* حذف
* مشاركة
* إعدادات
* تقارير
* المستخدمون

Avoid unnecessarily long commands.

10. Keep Action Buttons Short

Incorrect:

* اضغط هنا لإرسال النموذج

Preferred:

* إرسال

Incorrect:

* اضغط هنا للمتابعة

Preferred:

* متابعة

11. Use Natural Progress Messages

Incorrect:

* نحن نقوم بتحميل البيانات

Preferred:

* جارٍ تحميل البيانات

Incorrect:

* نحن نقوم بالمزامنة

Preferred:

* جارٍ المزامنة

The pattern جارٍ + المصدر is preferred for ongoing operations.

12. Write Natural Error Messages

Error messages should explain the problem directly.

Weak:

* حدث خطأ

Better:

* تعذر حفظ الملف
* تعذر الاتصال بالخادم
* كلمة المرور غير صحيحة

Always describe the failed action when possible.

13. Use Consistent Terminology

Choose one approved term and use it everywhere.

Recommended:

* Account → حساب
* Settings → الإعدادات
* Profile → الملف الشخصي
* Dashboard → لوحة التحكم
* Notification → إشعار
* Attachment → مرفق
* Folder → مجلد
* Search → بحث
* Filter → تصفية
* Download → تنزيل
* Upload → رفع
* Sign In → تسجيل الدخول
* Sign Out → تسجيل الخروج

Avoid mixing synonyms across the product.

14. Avoid Unnecessary Possessive Pronouns

English often uses "your" where Arabic does not.

Incorrect:

* كلمة المرور الخاصة بك
* ملفك الشخصي
* إعداداتك

Preferred:

* كلمة المرور
* الملف الشخصي
* الإعدادات

Retain possession only when needed for clarity.

15. Respect Arabic Gender Neutrality

Address users without assuming gender whenever possible.

Prefer:

* تسجيل الدخول
* المتابعة
* حفظ التغييرات

Instead of:

* سجّل دخولك
* تابِع
* احفظ تغييراتك

Use neutral labels rather than gendered imperatives.

16. Use Arabic Punctuation Correctly

Preferred punctuation:

* ،
* ؛
* ؟

Example:
هل تريد حفظ التغييرات؟

17. Avoid Excessive Formality

Do not translate enterprise software into bureaucratic Arabic.

Overly Formal:

* يرجى التكرم بإدخال بيانات الاعتماد الخاصة بكم

Preferred:

* أدخل بيانات تسجيل الدخول

Clarity is more important than formality.

18. Optimize for Scanability

UI text should be readable in seconds.

Preferred:

* حفظ
* حذف
* مشاركة
* تصدير
* استيراد

Not:

* اضغط هنا لحفظ البيانات
* اضغط هنا لحذف العنصر المحدد

19. Prefer Result-Oriented Status Messages

Instead of:

* تم الانتهاء من عملية التحميل

Use:

* اكتمل التحميل

Instead of:

* تم الانتهاء من المزامنة

Use:

* اكتملت المزامنة

Shorter and more natural.

20. Maintain RTL-Native Writing

Do not preserve English punctuation, capitalization patterns, spacing conventions, or layout structures.

Arabic UI text should feel designed for Arabic first, not adapted from English.

Golden Rule

If a native Arabic-speaking user can immediately tell that a string was translated from English, rewrite it.

Professional Arabic localization should be:

* concise
* grammatically native
* terminology-consistent
* gender-neutral where possible
* free from literal English syntax
* optimized for UI readability rather than linguistic completeness
