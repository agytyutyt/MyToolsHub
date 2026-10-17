import com.android.build.gradle.internal.api.BaseVariantOutputImpl

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jztools.infoparse"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jztools.infoparse"
        minSdk = 29
        targetSdk = 34
        versionCode = 6          // 覆盖安装/升级只认它，必须比上一版大（打包手册 §43）
        // 2026-10-16 全产品版本号归到 1.0.0 基线：versionName 回退到 1.0.0 不影响覆盖安装，
        // 但 versionCode 不能跟着回退，否则安卓报「已存在同版本或更高版本」。
        // 2026-10-17：versionCode 5 已随 v1.0.0 包发出（10-16 09:30），本轮安卓侧修复
        // （识别页移除「重置」）必须 5→6，否则目标机拒装。
        versionName = "1.0.0"
    }

    // full：全 ABI（含 x86 系，模拟器可用）；lite：仅 arm64-v8a + 中英文资源，真机分发用，体积约减半
    flavorDimensions += "edition"
    productFlavors {
        create("full") {
            dimension = "edition"
        }
        create("lite") {
            dimension = "edition"
            versionNameSuffix = "-lite"
            ndk { abiFilters += "arm64-v8a" }
            resourceConfigurations += listOf("zh", "zh-rCN", "en")
        }
    }

    // 正式签名（内部离线分发，密钥随仓库管理；丢失可用 keytool 重新生成但旧包将无法覆盖安装）
    signingConfigs {
        create("release") {
            storeFile = rootProject.file("release.keystore")
            storePassword = "infoparse2024"
            keyAlias = "infoparse"
            keyPassword = "infoparse2024"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // 产物命名带版本号：InfoParse-<versionName>-<buildType>.apk（lite 的 versionName 自带 -lite 后缀）
    applicationVariants.all {
        val vName = versionName
        val btName = buildType.name
        outputs.all {
            (this as BaseVariantOutputImpl).outputFileName = "InfoParse-$vName-$btName.apk"
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")

    // 相机（CameraX）
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")

    // 扫码：ML Kit bundled 内置模型（无需 GMS，国产手机可用，见 FAQ-2）
    implementation("com.google.mlkit:barcode-scanning:17.3.0")

    // JSON
    implementation("com.google.code.gson:gson:2.11.0")

    // T10：LZMA2(XZ) 解压（桌面端 v2 试压可能选 xz，两端算法集必须对齐）
    implementation("org.tukaani:xz:1.10")

    testImplementation("junit:junit:4.13.2")
}
