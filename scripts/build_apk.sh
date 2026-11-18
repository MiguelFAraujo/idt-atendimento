#!/bin/bash
set -e

BUILD_TOOLS="/home/miguel/Android/Sdk/build-tools/34.0.0"
ANDROID_JAR="/home/miguel/Android/Sdk/platforms/android-34/android.jar"
KEYSTORE="/home/miguel/.android/debug.keystore"
PROJECT_DIR="/home/miguel/services/idt-atendimento"
WORK_DIR="/tmp/idt_apk_build"

echo "=== 1. Limpando workspace de build ==="
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/classes"

echo "=== 2. Compilando recursos com aapt2 ==="
"$BUILD_TOOLS/aapt2" compile --dir "$PROJECT_DIR/android/res" -o "$WORK_DIR/res.zip"

echo "=== 3. Vinculando recursos e gerando R.java ==="
"$BUILD_TOOLS/aapt2" link -I "$ANDROID_JAR" \
    --manifest "$PROJECT_DIR/android/AndroidManifest.xml" \
    --min-sdk-version 24 \
    --target-sdk-version 34 \
    --version-code 4 \
    --version-name "2.2.1" \
    -o "$WORK_DIR/unaligned.apk" \
    --java "$PROJECT_DIR/android/src" \
    --auto-add-overlay \
    "$WORK_DIR/res.zip"

echo "=== 4. Compilando código Java ==="
javac -d "$WORK_DIR/classes" \
    -cp "$ANDROID_JAR" \
    "$PROJECT_DIR/android/src/com/inteligenciadotopo/atendimento/"*.java

echo "=== 5. Gerando classes.dex com D8 ==="
"$BUILD_TOOLS/d8" --output "$WORK_DIR/" \
    "$WORK_DIR/classes/com/inteligenciadotopo/atendimento/"*.class \
    --lib "$ANDROID_JAR"

echo "=== 6. Injetando classes.dex no APK ==="
(cd "$WORK_DIR" && zip -u unaligned.apk classes.dex)

echo "=== 7. Alinhando APK com zipalign ==="
"$BUILD_TOOLS/zipalign" -p -f 4 "$WORK_DIR/unaligned.apk" "$PROJECT_DIR/static/InteligenciaDoTopo.apk"

echo "=== 8. Assinando APK com apksigner ==="
"$BUILD_TOOLS/apksigner" sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --ks-key-alias androiddebugkey \
    "$PROJECT_DIR/static/InteligenciaDoTopo.apk"

echo "=== 9. Verificando assinatura do APK ==="
"$BUILD_TOOLS/apksigner" verify "$PROJECT_DIR/static/InteligenciaDoTopo.apk"

echo "=== SUCESSO: static/InteligenciaDoTopo.apk gerado e assinado! ==="
ls -lh "$PROJECT_DIR/static/InteligenciaDoTopo.apk"
