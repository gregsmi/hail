package is.hail.io.fs

import org.apache.commons.io.IOUtils
import org.scalatest.testng.TestNGSuite
import org.testng.SkipException
import org.testng.annotations.{BeforeClass, Test}

import java.io.FileInputStream

class GoogleStorageFSSuite extends TestNGSuite with FSSuite {
  @BeforeClass
  def beforeclass(): Unit = {
    if (System.getenv("HAIL_CLOUD") != "gcp") {
      throw new SkipException("This test suite is only run in GCP.");
    } else {
      assert(root != null)
      assert(fsResourcesRoot != null)
    }
  }

  lazy val fs = {
    val gac = System.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if (gac == null) {
      new GoogleStorageFS()
    } else {
      new GoogleStorageFS(
        Some(new String(IOUtils.toByteArray(new FileInputStream(gac)))))
    }
  }

  @Test def testMakeQualified(): Unit = {
    val qualifiedFileName = "gs://bucket/path"
    assert(fs.makeQualified(qualifiedFileName) == qualifiedFileName)

    val unqualifiedFileName = "not-gs://bucket/path"
    try {
      fs.makeQualified(unqualifiedFileName)
    }
    catch {
      case _: IllegalArgumentException =>
        return
    }
    assert(false)
  }

  @Test def testETag(): Unit = {
    val etag = fs.eTag(s"$fsResourcesRoot/a")
    assert(etag.nonEmpty)
  }

}
